from __future__ import annotations

from typing import Any

from app.domain.intent.adapter import adapt_session_view_to_intent_input
from app.domain.intent.schema import IntentRecognitionInput, IntentRecognitionOutput
from app.domain.intent.service import IntentRecognizer, intent_recognizer
from app.domain.intent_type import IntentType
from app.domain.session_context import SessionContextView
from app.domain.session.schema import SessionApplyIntentResult, SessionIntentResult, SessionIntentView, SessionState
from app.domain.session.service import SessionStateService, session_state_service


def adapt_intent_result_to_session(payload: Any) -> SessionIntentResult:
    """把 intent 模块输出转成 session 层可消费的契约。

    mode="json" 把 StrEnum（intent 层）等内部类型转成纯 JSON 值，
    避免枚举对象传入 session 的 Literal 字段产生兼容问题。
    """
    if isinstance(payload, SessionIntentResult):
        return payload
    if hasattr(payload, "model_dump"):
        return SessionIntentResult(**payload.model_dump(mode="json"))
    if isinstance(payload, dict):
        return SessionIntentResult(**payload)
    raise TypeError("Unsupported intent result payload for session adaptation.")


def adapt_session_state_to_intent_view(session_state: SessionState) -> SessionIntentView:
    """把 SessionState 投影成 intent 可消费的轻量视图，隐藏 session 内部细节。"""
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
    # 这条 pipeline 负责把 session 和 intent 串成一条完整链路：
    # session state -> session view -> intent input -> intent result -> session intent result -> apply back to session
    # 分工：
    #   - intent 层：只做"理解"（判意图 + 提取本轮 patch），不碰会话状态
    #   - session 层：只做"累积"（merge patch + 状态机推进），不直接调用 LLM
    def __init__(
        self,
        *,
        intent_service: IntentRecognizer | None = None,
        session_service: SessionStateService | None = None,
    ) -> None:
        # 默认注入全局单例，也允许测试/上层替换实现（依赖注入便于 mock）
        self.intent_service = intent_service or intent_recognizer
        self.session_service = session_service or session_state_service

    def run(
        self,
        *,
        session_state: SessionState,
        request_id: str,
        raw_message: str,
        user_context: dict[str, Any] | None = None,
    ) -> SessionApplyIntentResult:
        # 步骤0：空输入防护（raw_message 不允许空白，直接给 unknown 兜底，
        #        不构造 intent 输入、不调 LLM，避免 500 与浪费 token）
        if not (raw_message or "").strip():
            intent_output = IntentRecognitionOutput(intent_type=IntentType.UNKNOWN, reasoning="空输入。")
            return self.session_service.apply_intent_result(
                session_state,
                adapt_intent_result_to_session(intent_output),
            )

        # 步骤1：把 session 完整状态投影成 intent 可消费的轻量视图
        session_view = adapt_session_state_to_intent_view(session_state)

        # 步骤2：视图 + 本轮用户原话 + 用户偏好 → 拼成 LLM 能看的 intent 输入
        intent_input = adapt_session_view_to_intent_input(
            request_id=request_id,
            session_id=session_state.session_id,
            user_id=session_state.user_id,
            raw_message=raw_message,
            session_view=session_view,
            user_context=user_context,
        )

        # 步骤3：调意图识别（LLM 优先，失败走 fallback），得到 intent_type + 本轮 patch
        intent_output = self.intent_service.recognize(intent_input)

        # 步骤4：把 intent 模块输出转成 session 层可消费的 SessionIntentResult
        session_intent_result = adapt_intent_result_to_session(intent_output)

        # 步骤5：应用到 session —— merge patch 进累计需求 + 状态机决定下一阶段
        return self.session_service.apply_intent_result(session_state, session_intent_result)

    # ---- 以下三个方法供测试/诊断使用：把链路拆成"识别"与"应用"两段可单独调用 ----

    def build_intent_input(
        self,
        *,
        session_state: SessionState,
        request_id: str,
        raw_message: str,
        user_context: dict[str, Any] | None = None,
    ) -> IntentRecognitionInput:
        # 只走到步骤2，返回 intent 输入（便于断言输入内容是否正确）
        session_view = adapt_session_state_to_intent_view(session_state)
        return adapt_session_view_to_intent_input(
            request_id=request_id,
            session_id=session_state.session_id,
            user_id=session_state.user_id,
            raw_message=raw_message,
            session_view=session_view,
            user_context=user_context,
        )

    def recognize_only(
        self,
        *,
        session_state: SessionState,
        request_id: str,
        raw_message: str,
        user_context: dict[str, Any] | None = None,
    ) -> IntentRecognitionOutput:
        # 只做意图识别，不应用到 session（便于单独测试意图判定结果）
        intent_input = self.build_intent_input(
            session_state=session_state,
            request_id=request_id,
            raw_message=raw_message,
            user_context=user_context,
        )
        return self.intent_service.recognize(intent_input)

    def adapt_output_only(self, intent_output: IntentRecognitionOutput | dict[str, Any]) -> SessionIntentResult:
        # 只做 schema 转换，不应用（便于单独测试 adapter）
        return adapt_intent_result_to_session(intent_output)


intent_session_pipeline = IntentSessionPipeline()
