from __future__ import annotations

from datetime import datetime, timezone

from app.domain.common.chat import ChatMessage, ChatRole
from app.domain.common.stage import ConversationStage
from app.domain.session.merge import merge_request_state
from app.domain.session.schema import SessionApplyIntentResult, SessionIntentResult, SessionState

class SessionStateService:
    # 创建新的空会话
    def initialize(self, session_id: str, user_id: str | None = None) -> SessionState:
        now = self._now()
        return SessionState(session_id=session_id, user_id=user_id, created_at=now, updated_at=now)

    # 应用 intent 结果：patch merge + 阶段更新（会话层唯一状态写入点）
    def apply_intent_result(
        self,
        session_state: SessionState,
        intent_result: SessionIntentResult,
    ) -> SessionApplyIntentResult:
        # 1. merge：本轮 patch 累计进需求状态，算出还缺哪些字段
        #    （missing 基于 merge 后的 next_state 判定，而非旧 state）
        merge_result = merge_request_state(session_state.current_request_state, intent_result.extracted_request_patch)

        # 2. 深拷贝避免修改入参
        next_session = session_state.model_copy(deep=True)

        # 3. 写回 merge 结果
        next_session.current_request_state = merge_result.next_state          # 累计后的完整需求
        next_session.pending_questions = list(merge_result.remaining_missing_fields)  # 待追问字段

        # 4. 状态机推进（new_plan + 字段齐 → ready_to_plan 在 _decide_stage 内处理）
        next_session.conversation_stage = self._decide_stage(next_session, intent_result, merge_result.remaining_missing_fields)

        # 5. 改稿计数：仅执行改稿时 +1（缺字段的 revise_collecting 不算）；new_plan 重置
        if intent_result.intent_type == "revise_plan" and not merge_result.remaining_missing_fields:
            next_session.revision_count += 1
        elif intent_result.intent_type == "new_plan":
            next_session.revision_count = 0

        # 6. 生命周期：进入 closed 记录结束时间（仅一次）
        if next_session.conversation_stage == "closed" and not next_session.closed_at:
            next_session.closed_at = self._now()

        next_session.updated_at = self._now()

        return SessionApplyIntentResult(
            session_state=next_session,
            merge_result=merge_result,
            intent_result=intent_result,
        )

    # 追加消息到近期对话窗口（intent 上下文用，超出 max_items 截断）
    def append_recent_message(self, session_state: SessionState, role: ChatRole, content: str, max_items: int = 8) -> SessionState:
        next_session = session_state.model_copy(deep=True)
        if content.strip():
            next_session.recent_messages.append(ChatMessage(role=role, content=content))
        if len(next_session.recent_messages) > max_items:
            next_session.recent_messages = next_session.recent_messages[-max_items:]
        next_session.updated_at = self._now()
        return next_session

    # 状态机：按 intent_type + missing + 当前阶段决定下一阶段
    # 优先级：end_session > confirm > qa/reject > revise_plan > 缺字段 > new_plan > 保持当前
    def _decide_stage(self, session_state: SessionState, intent_result: SessionIntentResult, missing_fields: list[str]) -> ConversationStage:
        # 显式结束 → closed
        if intent_result.intent_type == "end_session":
            return "closed"
        # 确认：有产物才收尾 completed（无产物视为 LLM 误判，不推进）
        if intent_result.intent_type == "confirm" and session_state.artifacts.has_plan:
            return "completed"
        # 问答/拒绝：仅对话型阶段写回 qa，不覆盖 completed 等持久阶段
        if intent_result.intent_type in ("qa", "reject"):
            if session_state.conversation_stage in (
                "qa", "collecting_destination", "collecting_dates", "collecting_requirements",
            ):
                return "qa"
            return session_state.conversation_stage
        # 改稿：缺信息继续收集，否则就绪执行
        if intent_result.intent_type == "revise_plan":
            return "revise_collecting" if missing_fields else "revise_ready"
        # 缺关键字段 → 进入对应 collecting 阶段（优先级：目的地 > 日期 > 其他）
        # REQUIRED_FIELDS 现仅含 destination/start_date/end_date，collecting_requirements 暂不可达
        if missing_fields:
            if "destination" in missing_fields:
                return "collecting_destination"
            if "start_date" in missing_fields or "end_date" in missing_fields:
                return "collecting_dates"
            return "collecting_requirements"
        # 字段齐 + new_plan → ready_to_plan
        if intent_result.intent_type == "new_plan":
            return "ready_to_plan"
        # 保持当前阶段，避免闲聊(unknown)误触发重新规划
        return session_state.conversation_stage

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

session_state_service = SessionStateService()
