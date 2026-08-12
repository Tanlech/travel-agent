from __future__ import annotations

import json
from uuid import uuid4

from app.agents.planning import planning_agent
from app.agents.revise import revise_agent
from app.agents.schema.orchestrator import AgentRequest, AgentResponse, DialogueDecision, ExecutionPlan
from app.agents.schema.planning import PlanInput, PlanningRequest, TripPlan
from app.agents.schema.revise import RevisionIntent, ReviseAgentInput, ReviseExecutionPolicy, ReviseSessionContext, ReviseUserContext
from app.budgets.policy import default_budget_policy
from app.budgets.tracker import TokenBudgetTracker
from app.domain.context.response import ResponseContext
from app.domain.session.mapper import (
    build_follow_up_question,
    build_plan_summary,
    session_context_to_revise_session_context,
    session_state_to_planning_request,
    session_state_to_session_context,
    stage_to_execution_mode,
    user_context_to_revise_user_context,
)
from app.domain.session.pipeline import intent_session_pipeline
from app.domain.session.repository_impl import redis_session_repository
from app.domain.session.service import session_state_service
from app.infrastructure.llm.client import get_llm_client
from app.infrastructure.llm.schemas import ItineraryDraftSchema
from app.domain.memory.manager import memory_manager
from app.observability.tracing import new_trace_id


# qa 分支的自由对话 system prompt：
# LLM 以旅行助手身份回答用户问题，可参考当前行程摘要与近期对话，
# 但不主动触发规划/改稿（那是意图识别的职责）。
QA_CHAT_SYSTEM_PROMPT = """
你是旅行规划助手，正在和用户进行日常对话。
你可以回答与旅行相关的任何问题（目的地建议、天气、美食、景点、行程相关疑问等），
也可以结合"当前行程摘要"和"近期对话"上下文回答。
要求：
- 语气自然、简洁，像真人助手，不要输出 JSON 或结构化标记。
- 只在用户明确要求修改/生成行程时，才提示"我可以帮你重新规划/调整行程"，
  不要自行假设要改行程。
- 如果不知道，坦诚说不知道，不要编造。
""".strip()


class PlanningOrchestrator:
    """对话决策的兼容入口。

    新流程里对话阶段由 SessionStateService._decide_stage 决定，但 planning_agent.run_pipeline
    内部仍会调用本方法做一次字段校验，因此保留并去掉 budget 强制（与 intent/merge 对齐，
    关键字段为 destination + days）。
    """

    def resolve(self, request: PlanningRequest, session=None) -> tuple[DialogueDecision, ResponseContext, object]:
        from app.domain.context.session import SessionContext

        current_session = session or SessionContext()
        missing: list[str] = []
        if not str(request.destination or "").strip():
            missing.append("destination")
        if int(request.days or 0) <= 0:
            missing.append("days")

        field_labels = {
            "destination": "目的地城市",
            "days": "旅行天数",
        }

        destination = str(request.destination or "").strip() or current_session.last_destination
        is_same_destination = bool(destination and current_session.last_destination and destination == current_session.last_destination)
        stored_plan, stored_draft = redis_session_repository.load_artifacts(current_session.session_id)
        has_revision_artifacts = bool(stored_plan and stored_draft)
        has_prior_plan = has_revision_artifacts or current_session.revision_count > 0 or current_session.conversation_stage == "revise_plan"
        has_explicit_revision_message = bool(str(request.revision_message or "").strip())

        if not missing:
            current_session.pending_questions = []
            current_session.confirmed_fields = ["destination", "days"]
            if current_session.last_destination and is_same_destination and has_prior_plan and has_explicit_revision_message:
                current_session.conversation_stage = "revise_plan"
                current_session.revision_count += 1
                return DialogueDecision(status="ready_to_revise"), ResponseContext(response_mode="revise_plan", needs_follow_up=False), current_session
            current_session.conversation_stage = "new_plan"
            current_session.last_destination = destination
            return DialogueDecision(status="ready_to_plan"), ResponseContext(response_mode="final_plan", needs_follow_up=False), current_session

        current_session.pending_questions = [field_labels[item] for item in missing]
        current_session.conversation_stage = "clarification"
        question = "请补充以下信息后我再开始规划：" + "、".join(current_session.pending_questions)
        return (
            DialogueDecision(status="need_clarification", missing_fields=missing, follow_up_question=question),
            ResponseContext(response_mode="follow_up", needs_follow_up=True, include_alternatives=False),
            current_session,
        )


class TravelOrchestrator:
    """对话编排入口。

    落地版本：通过 IntentSessionPipeline 串联意图识别与会话状态，SessionState 落 Redis，
    按 conversation_stage 路由到 clarify / planning / revise / qa 四个分支。
    """

    def handle(self, agent_request: AgentRequest) -> AgentResponse:
        trace_id = new_trace_id()
        budget_tracker = TokenBudgetTracker(default_budget_policy())
        session_id = agent_request.session_id or str(uuid4())
        user_id = agent_request.user_id

        # 1. 载入或初始化会话状态（Redis 持久化，跨进程可用）
        session_state = redis_session_repository.load(session_id) or session_state_service.initialize(session_id, user_id)

        # 1.5 填充已有行程摘要（latest_plan_summary）：
        #     意图识别（尤其 LLM 路径）靠它区分 new_plan（首次规划）vs revise_plan（改稿），
        #     若 SessionState 里还没有摘要，则从 Redis artifacts 的 plan 生成一份轻量摘要
        if session_state.artifacts.plan_summary is None:
            stored_plan, _ = redis_session_repository.load_artifacts(session_id)
            if stored_plan:
                session_state.artifacts.plan_summary = build_plan_summary(stored_plan)

        # 2. 记录用户本轮消息
        session_state = session_state_service.append_recent_message(session_state, "user", agent_request.message)

        # 3. 意图识别 + 应用到会话状态（pipeline 串联 session↔intent）
        # user_context 传入真实用户偏好（从 memory + 当前累计需求推断），供 LLM 意图识别参考
        _req_for_ctx = session_state_to_planning_request(session_state)
        _user_context = memory_manager.build_user_context(_req_for_ctx, user_id=user_id).model_dump()
        result = intent_session_pipeline.run(
            session_state=session_state,
            request_id=agent_request.request_id,
            raw_message=agent_request.message,
            user_context=_user_context,
        )
        session_state = result.session_state
        intent = result.intent_result

        mode = stage_to_execution_mode(session_state.conversation_stage)
        app_logger.info("orchestrator_start", request_id=agent_request.request_id, trace_id=trace_id, mode=mode)
        metrics_recorder.record("orchestrator_requests", 1, mode=mode)

        # 分支一：信息不齐，追问补全
        if mode == "clarify":
            follow_up = build_follow_up_question(session_state.pending_questions)
            session_state = session_state_service.append_recent_message(session_state, "assistant", follow_up)
            redis_session_repository.save(session_state)
            return AgentResponse(
                request_id=agent_request.request_id,
                session_id=session_id,
                status="needs_follow_up",
                mode="clarify",
                follow_up_question=follow_up,
                trace_id=trace_id,
                metrics={"token_budget": budget_tracker.allocate(300)},
                debug={"intent": intent.model_dump(), "stage": session_state.conversation_stage},
            )

        # 分支二：执行旅行规划
        if mode == "planning":
            planning_request = session_state_to_planning_request(session_state)
            try:
                plan_result = planning_agent.run_pipeline(PlanInput(request=planning_request, user_id=user_id, session_id=session_id))
            except Exception as exc:
                app_logger.error("planning_failed", request_id=agent_request.request_id, trace_id=trace_id, error=str(exc))
                msg = "行程规划失败，请稍后重试或补充更多信息。"
                session_state = session_state_service.append_recent_message(session_state, "assistant", msg)
                redis_session_repository.save(session_state)
                return AgentResponse(
                    request_id=agent_request.request_id,
                    session_id=session_id,
                    status="failed",
                    mode="planning",
                    summary=msg,
                    trace_id=trace_id,
                    debug={"intent": intent.model_dump(), "stage": session_state.conversation_stage, "error": str(exc)},
                )
            plan = plan_result.get("plan")
            draft = plan_result.get("final_draft")
            summary = (plan_result.get("final_decision") or {}).get("summary") or "已为你生成旅行行程。"
            # 写回会话产物（完整 plan/draft 存 Redis artifacts，摘要存 SessionState）
            session_state.artifacts.has_current_plan = bool(plan)
            session_state.artifacts.has_current_draft = bool(draft)
            if plan:
                # 生成新摘要供下一轮意图识别使用（覆盖旧值）
                session_state.artifacts.plan_summary = build_plan_summary(plan.model_dump())
                redis_session_repository.save_artifacts(session_id, plan.model_dump(), draft.model_dump() if draft else None)
            session_state = session_state_service.append_recent_message(session_state, "assistant", summary)
            redis_session_repository.save(session_state)
            return AgentResponse(
                request_id=agent_request.request_id,
                session_id=session_id,
                status="completed",
                mode="planning",
                summary=summary,
                plan=plan.model_dump() if plan else None,
                draft=draft.model_dump() if draft else None,
                trace_id=trace_id,
                metrics={"token_budget": budget_tracker.allocate(5000), "remaining_budget": budget_tracker.remaining()},
                debug={"intent": intent.model_dump(), "stage": session_state.conversation_stage},
            )

        # 分支三：更新已有旅行（改稿）
        if mode == "revise":
            plan_payload, draft_payload = redis_session_repository.load_artifacts(session_id)
            if not plan_payload or not draft_payload:
                msg = "当前会话还没有可修改的行程，请先让我为你生成一份。"
                session_state = session_state_service.append_recent_message(session_state, "assistant", msg)
                redis_session_repository.save(session_state)
                return AgentResponse(
                    request_id=agent_request.request_id,
                    session_id=session_id,
                    status="failed",
                    mode="revise",
                    summary=msg,
                    trace_id=trace_id,
                    debug={"intent": intent.model_dump(), "stage": session_state.conversation_stage},
                )
            planning_request = session_state_to_planning_request(session_state)
            session_context = session_state_to_session_context(session_state)
            revise_input = ReviseAgentInput(
                request=planning_request,
                user_context=user_context_to_revise_user_context(_user_context),
                session_context=session_context_to_revise_session_context(session_context),
                execution_policy=ReviseExecutionPolicy(),
                current_plan=TripPlan(**plan_payload),
                current_draft=ItineraryDraftSchema(**draft_payload),
                revision_intent=RevisionIntent(
                    user_message=agent_request.message,
                    change_scope=intent.revision_scope_hint or "day_level",
                    revision_goal=agent_request.message,
                ),
                bootstrap_intent={
                    "change_scope": intent.revision_scope_hint,
                    "revision_message": agent_request.message,
                },
            )
            revise_result = None
            try:
                revise_result = revise_agent.run(revise_input)
            except Exception as exc:
                app_logger.error("revise_failed", request_id=agent_request.request_id, trace_id=trace_id, error=str(exc))
                msg = "行程修改失败，请稍后重试或换个说法。"
                session_state = session_state_service.append_recent_message(session_state, "assistant", msg)
                redis_session_repository.save(session_state)
                return AgentResponse(
                    request_id=agent_request.request_id,
                    session_id=session_id,
                    status="failed",
                    mode="revise",
                    summary=msg,
                    trace_id=trace_id,
                    debug={"intent": intent.model_dump(), "stage": session_state.conversation_stage, "error": str(exc)},
                )
            redis_session_repository.save_artifacts(
                session_id,
                revise_result.artifacts.plan.model_dump(),
                revise_result.artifacts.draft.model_dump(),
            )
            # 改稿后更新摘要：下一轮意图识别/再次改稿基于最新版本判断
            session_state.artifacts.plan_summary = build_plan_summary(revise_result.artifacts.plan.model_dump())
            summary = revise_result.summary or "已按你的要求更新行程。"
            session_state = session_state_service.append_recent_message(session_state, "assistant", summary)
            redis_session_repository.save(session_state)
            return AgentResponse(
                request_id=agent_request.request_id,
                session_id=session_id,
                status="completed",
                mode="revise",
                summary=summary,
                plan=revise_result.artifacts.plan.model_dump(),
                draft=revise_result.artifacts.draft.model_dump(),
                trace_id=trace_id,
                metrics={"token_budget": budget_tracker.allocate(4000), "remaining_budget": budget_tracker.remaining()},
                debug={
                    "intent": intent.model_dump(),
                    "stage": session_state.conversation_stage,
                    "revision_summary": revise_result.revision_summary,
                },
            )

        # 分支四：问答 / 确认 / 拒绝 / 结束
        if intent.intent_type == "confirm":
            summary = intent.reasoning or "好的，行程已确认。"
        elif intent.intent_type == "reject":
            # 用户明确表示不需要/算了：直接回绝，不走 LLM 自由对话（避免语义错乱）
            summary = intent.reasoning or "好的，那就不规划了。有需要随时找我。"
        elif intent.intent_type == "end_session":
            summary = intent.reasoning or "好的，祝你旅途愉快！再见。"
        else:
            # qa（正常 AI 对话）：走真正的 LLM 自由对话，带行程摘要上下文
            # LLM 不可用/失败时降级到 intent 的 reasoning 话术
            summary = self._qa_chat_reply(agent_request.message, session_state) or intent.reasoning or "我已记录你的消息。"
        session_state = session_state_service.append_recent_message(session_state, "assistant", summary)
        redis_session_repository.save(session_state)
        return AgentResponse(
            request_id=agent_request.request_id,
            session_id=session_id,
            status="completed",
            mode="qa",
            summary=summary,
            trace_id=trace_id,
            metrics={"token_budget": budget_tracker.allocate(500)},
            debug={"intent": intent.model_dump(), "stage": session_state.conversation_stage},
        )

    # qa 分支的自由对话：用 LLM 认真回答用户问题
    # 输入：用户原话 + 当前会话状态（行程摘要/近期对话）
    # 输出：纯文本回复；LLM 不可用/失败时返回 None，由调用方降级
    def _qa_chat_reply(self, user_message: str, session_state) -> str | None:
        llm_client = get_llm_client()
        if not llm_client.is_enabled():
            return None

        # 组装上下文：行程摘要（如果有）+ 近期对话（最近 6 条）
        plan_summary = session_state.artifacts.plan_summary or {}
        context_payload = {
            "current_plan": {
                "destination": plan_summary.get("destination"),
                "days": plan_summary.get("days"),
                "summary": (plan_summary.get("summary") or "")[:300],
            } if plan_summary else None,
            "recent_messages": session_state.recent_messages[-6:],
            "user_message": user_message,
        }
        user_prompt = json.dumps(context_payload, ensure_ascii=False, indent=2)

        reply = llm_client.generate_chat_reply(
            system_prompt=QA_CHAT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        return reply


planning_orchestrator = PlanningOrchestrator()
travel_orchestrator = TravelOrchestrator()
