from __future__ import annotations

from datetime import datetime, timezone

from app.domain.intent.schema import ChatMessage
from app.domain.session.merge import merge_request_state
from app.domain.session.schema import SessionApplyIntentResult, SessionIntentResult, SessionState


class SessionStateService:
    # 创建一个新的空 session state
    def initialize(self, session_id: str, user_id: str | None = None) -> SessionState:
        now = self._now_iso()
        return SessionState(session_id=session_id, user_id=user_id, created_at=now, updated_at=now)

    # 把 intent 结果应用到当前 session，统一完成 patch merge 和阶段更新
    # 这是会话层的核心写入点：所有状态变更都经这里完成
    def apply_intent_result(
        self,
        session_state: SessionState,
        intent_result: SessionIntentResult,
    ) -> SessionApplyIntentResult:
        # 1. merge：把本轮 patch 累计进需求状态，并算出还缺哪些字段
        #    注意：_decide_stage 用 next_state（merge后）判断 missing，不是旧 state —— 这是之前修过的关键 bug
        merge_result = merge_request_state(session_state.current_request_state, intent_result.extracted_request_patch)

        # 2. 深拷贝一份 session，避免修改入参（不可变更新风格，调用方拿到的仍是旧对象）
        next_session = session_state.model_copy(deep=True)

        # 3. 把 merge 结果写回 session 各字段
        next_session.current_request_state = merge_result.next_state          # 累计后的完整需求
        next_session.pending_questions = list(merge_result.remaining_missing_fields)  # 待追问字段

        # 4. 状态机：根据 intent_type + missing + 当前阶段，决定下一阶段
        #    （new_plan + 字段齐 会在 _decide_stage 内返回 ready_to_plan）
        next_session.conversation_stage = self._decide_stage(next_session, intent_result, merge_result.remaining_missing_fields)

        # 5. 改稿计数：revise_plan 时递增（后续可用于限制最大改稿次数等策略）
        if intent_result.intent_type == "revise_plan":
            next_session.revision_count += 1

        next_session.updated_at = self._now_iso()

        return SessionApplyIntentResult(
            session_state=next_session,
            merge_result=merge_result,
            intent_result=intent_result,
        )

    # 追加一条消息到 recent_messages，超出上限自动截断保留最近 max_items 条
    # 这是给 intent 上下文用的近期对话窗口，不是完整聊天记录
    def append_recent_message(self, session_state: SessionState, role: str, content: str, max_items: int = 8) -> SessionState:
        next_session = session_state.model_copy(deep=True)
        if content.strip():
            next_session.recent_messages.append(ChatMessage(role=role, content=content))
        if len(next_session.recent_messages) > max_items:
            next_session.recent_messages = next_session.recent_messages[-max_items:]
        next_session.updated_at = self._now_iso()
        return next_session

    # 状态机：根据 intent_type + missing_fields + 当前阶段，决定 conversation_stage
    # 优先级：end_session > qa > revise_plan > 缺字段 > new_plan > 保持当前 > 按字段推断
    def _decide_stage(self, session_state: SessionState, intent_result: SessionIntentResult, missing_fields: list[str]) -> str:
        # 显式结束 → closed
        if intent_result.intent_type == "end_session":
            return "closed"
        # 确认当前行程 → completed（会话收尾，避免误触发重新规划/改稿）
        if intent_result.intent_type == "confirm":
            return "completed"
        # 问答 → qa（不改变需求状态，仅对话）
        if intent_result.intent_type == "qa":
            return "qa"
        # 拒绝（不用了/算了）：回到闲聊，不推进规划/改稿，
        # 避免用户明确拒绝后因字段已齐仍触发规划执行
        if intent_result.intent_type == "reject":
            return "qa"
        # 改稿：还缺信息则继续收集，否则就绪等待执行
        if intent_result.intent_type == "revise_plan":
            return "revise_collecting" if missing_fields else "revise_ready"
        # 缺关键字段 → 进入对应的 collecting 阶段（按优先级：目的地 > 日期 > 其他）
        if missing_fields:
            if "destination" in missing_fields:
                return "collecting_destination"
            if "start_date" in missing_fields or "end_date" in missing_fields:
                return "collecting_dates"
            return "collecting_requirements"
        # 字段齐 + 明确新规划 → ready_to_plan
        if intent_result.intent_type == "new_plan":
            return "ready_to_plan"
        # unknown 及其他未显式处理的意图：保持当前阶段，不主动按字段推断，
        # 避免"字段恰好齐 + 一句闲聊(unknown)"被误判成 ready_to_plan 触发重新规划
        return session_state.conversation_stage

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()


session_state_service = SessionStateService()
