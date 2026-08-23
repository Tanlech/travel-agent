from __future__ import annotations

import json
import re
from uuid import uuid4

from app.agents.planning import planning_agent
from app.agents.revise import revise_agent
from app.agents.schema.orchestrator import AgentRequest, AgentResponse, DialogueDecision, ExecutionPlan
from app.agents.schema.planning import PlanInput, PlanningRequest, TripPlan
from app.agents.schema.revise import RevisionIntent, ReviseAgentInput, ReviseExecutionPolicy
from app.observability.token_budget import TokenBudgetTracker, default_budget_policy
from app.domain.common.planning import compute_missing_fields, extract_plan_attractions
from app.domain.common.time import utc_now
from app.domain.common.user import UserContext
from app.domain.context.response import ResponseContext
from app.domain.memory.manager import memory_manager
from app.domain.session.mapper import (
    build_follow_up_question,
    build_plan_summary,
    session_state_to_planning_request,
    session_state_to_session_context,
    stage_to_execution_mode,
)
from app.domain.session.pipeline import intent_session_pipeline
from app.domain.session.repository import redis_session_repository
from app.domain.session.schema import SessionIntentResult, SessionState
from app.domain.session.service import session_state_service
from app.domain.common.itinerary import ItineraryDraftSchema
from app.infrastructure.llm_client import get_llm_client
from app.observability.monitoring import app_logger, metrics_recorder
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
    """planning_agent 内部的只读校验入口。

    新流程里对话阶段完全由 SessionStateService._decide_stage 决定，planning 分支只在
    stage=ready_to_plan 时进入（关键字段已齐）。因此这里不再做任何状态修改
    （不写 pending_questions / conversation_stage / revision_count / last_destination），
    只保留最后一道字段校验兜底，口径与 REQUIRED_FIELDS 单一来源一致。
    """

    def resolve(self, request: PlanningRequest, session=None) -> tuple[DialogueDecision, ResponseContext]:
        field_labels = {
            "destination": "目的地城市",
            "start_date": "游玩开始日期",
            "end_date": "游玩结束日期",
        }
        missing = compute_missing_fields(request)
        if not missing:
            return (
                DialogueDecision(status="ready_to_plan"),
                ResponseContext(response_mode="final_plan"),
            )
        question = "请补充以下信息后我再开始规划：" + "、".join(field_labels.get(f, f) for f in missing)
        return (
            DialogueDecision(status="need_clarification", missing_fields=missing, follow_up_question=question),
            ResponseContext(response_mode="follow_up", include_alternatives=False),
        )


# 空泛改稿预检：具体修改动词（命中即认为原话含修改指令）。
# 典型空泛表达（"改一下"/"修改"/"调整下"）由 _EMPTY_REVISE_PATTERN 单独拦截，
# 避免"改一下"因命中动词而被误判为有具体指令。
_REVISION_SIGNAL_KEYWORDS = (
    "改", "换", "调整", "修改", "更改", "删", "去掉", "增加", "添加",
    "提前", "推迟", "延长", "缩短", "重新", "优化",
)

# 仅由"修改动词 + 可选语气词"构成、不含具体宾语的空泛改稿（如"改一下"/"修改"/"调整下"）
_EMPTY_REVISE_PATTERN = re.compile(
    r"^(?:修改|调整|改|换|优化|更改|变更|重排|重做)(?:一下|下|一改|一换|吧|呗|点|了|一遍)?$"
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

        # 1. 载入会话 + 意图识别 + 状态合并 + 乐观保存（含并发冲突重试）
        session_state, intent, _user_context, trip_history, prefetched_artifacts = self._resolve_intent(
            session_id=session_id,
            user_id=user_id,
            request_id=agent_request.request_id,
            raw_message=agent_request.message,
        )

        mode = stage_to_execution_mode(session_state.conversation_stage)
        app_logger.info("orchestrator_start", request_id=agent_request.request_id, trace_id=trace_id, mode=mode)
        metrics_recorder.record("orchestrator_requests", 1, mode=mode)

        # 分支一：信息不齐，追问补全
        if mode == "clarify":
            follow_up = build_follow_up_question(session_state.pending_questions)
            session_state = session_state_service.append_recent_message(session_state, "assistant", follow_up)
            self._save_or_log(session_state, request_id=agent_request.request_id, trace_id=trace_id)
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
                self._save_or_log(session_state, request_id=agent_request.request_id, trace_id=trace_id)
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
            # 写回会话产物（完整 plan/draft 与 state 原子提交，摘要存 SessionState）
            if plan:
                # 生成新摘要供下一轮意图识别使用（覆盖旧值）
                session_state.artifacts.plan_summary = build_plan_summary(plan.model_dump())
                # 产物标记：已成功生成，revise 分支据此区分"从未生成"与"产物丢失"
                session_state.artifacts.has_plan = True
                session_state.artifacts.plan_updated_at = utc_now()
            # 规划完成，推进阶段，避免后续闲聊(unknown)被误判成 ready_to_plan 触发重新规划
            session_state.conversation_stage = "completed"
            session_state = session_state_service.append_recent_message(session_state, "assistant", summary)
            # 原子提交：state 与产物同事务，冲突整体回滚，避免"新 plan + 旧摘要"错位；
            # 冲突时以最新会话为基底重试，避免产物静默丢失导致下次请求重复规划。
            # plan 为 None（规划失败）时不动产物，仅保存 state（保留既有产物）
            if plan:
                saved, session_state = self._commit_artifacts_with_retry(
                    session_state,
                    plan.model_dump(),
                    draft.model_dump() if draft else None,
                    summary,
                )
            else:
                saved = redis_session_repository.save(session_state) is not None
            if not saved:
                app_logger.warning(
                    "session_save_conflict_dropped",
                    request_id=agent_request.request_id,
                    trace_id=trace_id,
                    session_id=session_id,
                )
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
            # 复用 _resolve_intent 预读取的产物（仅在 plan_summary 缺失时才预读），避免同请求内重复读 Redis
            plan_payload, draft_payload = (
                prefetched_artifacts if prefetched_artifacts is not None else redis_session_repository.load_artifacts(session_id)
            )
            if not plan_payload or not draft_payload:
                # 区分"从未生成过行程"与"产物已过期丢失"，给出不同引导
                if session_state.artifacts.has_plan:
                    msg = "之前的行程数据已过期，请让我重新为你生成一份。"
                    # 产物已丢：连摘要一起清空，避免意图层仍凭 plan_summary 判定"已有行程"
                    # 导致下一轮 revise 又走进这里，形成"反复提示已过期"的死循环
                    session_state.artifacts.has_plan = False
                    session_state.artifacts.plan_summary = None
                    session_state.artifacts.plan_updated_at = None
                else:
                    msg = "当前会话还没有可修改的行程，请先让我为你生成一份。"
                session_state = session_state_service.append_recent_message(session_state, "assistant", msg)
                self._save_or_log(session_state, request_id=agent_request.request_id, trace_id=trace_id)
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

            # 空泛改稿预检：既没提取出字段修改，原话也不含具体修改指令（如"改一下"）。
            # 此时直接执行会产出无意义的整段重写，应追问具体要改哪部分
            if not intent.extracted_request_patch and not self._has_revision_signal(agent_request.message):
                msg = "请告诉我具体想调整哪里，比如换住宿、改景点或调整日期。"
                session_state = session_state_service.append_recent_message(session_state, "assistant", msg)
                self._save_or_log(session_state, request_id=agent_request.request_id, trace_id=trace_id)
                return AgentResponse(
                    request_id=agent_request.request_id,
                    session_id=session_id,
                    status="needs_follow_up",
                    mode="revise",
                    follow_up_question=msg,
                    summary=msg,
                    trace_id=trace_id,
                    metrics={"token_budget": budget_tracker.allocate(300)},
                    debug={"intent": intent.model_dump(), "stage": session_state.conversation_stage},
                )

            revise_input = ReviseAgentInput(
                request=planning_request,
                user_context=UserContext(**_user_context),
                session_context=session_context,
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
                # 复用 _resolve_intent 已加载的 trip_history，避免同请求重复读 Redis
                trip_history=trip_history,
            )
            revise_result = None
            try:
                revise_result = revise_agent.run(revise_input)
            except Exception as exc:
                app_logger.error("revise_failed", request_id=agent_request.request_id, trace_id=trace_id, error=str(exc))
                msg = "行程修改失败，请稍后重试或换个说法。"
                session_state = session_state_service.append_recent_message(session_state, "assistant", msg)
                self._save_or_log(session_state, request_id=agent_request.request_id, trace_id=trace_id)
                return AgentResponse(
                    request_id=agent_request.request_id,
                    session_id=session_id,
                    status="failed",
                    mode="revise",
                    summary=msg,
                    trace_id=trace_id,
                    debug={"intent": intent.model_dump(), "stage": session_state.conversation_stage, "error": str(exc)},
                )
            # 改稿后更新摘要：下一轮意图识别/再次改稿基于最新版本判断
            session_state.artifacts.plan_summary = build_plan_summary(revise_result.artifacts.plan.model_dump())
            session_state.artifacts.has_plan = True
            session_state.artifacts.plan_updated_at = utc_now()
            summary = revise_result.summary or "已按你的要求更新行程。"
            # 改稿完成，推进阶段（连续改稿仍靠 latest_plan_summary 判断，不受此影响）
            session_state.conversation_stage = "completed"
            session_state = session_state_service.append_recent_message(session_state, "assistant", summary)
            # 原子提交：state 与改稿产物同事务，冲突整体回滚；冲突时以最新会话为基底重试，
            # 避免改稿产物静默丢失导致下次请求重复执行改稿
            saved, session_state = self._commit_artifacts_with_retry(
                session_state,
                revise_result.artifacts.plan.model_dump(),
                revise_result.artifacts.draft.model_dump(),
                summary,
            )
            if not saved:
                app_logger.warning(
                    "session_save_conflict_dropped",
                    request_id=agent_request.request_id,
                    trace_id=trace_id,
                    session_id=session_id,
                )
            # 改稿消息中新表达的偏好并入长期记忆（与规划分支 persist_user_memory 对齐）：
            # 改稿常伴随偏好微调（如"换成都市的酒店"），不能只存在于本轮、规划分支却能沉淀；
            # 基于 merge 后的完整 request 重建 user_context，布尔/否定偏好
            # （如"不要乐园"→accept_theme_park=False）与规划分支走同一逻辑
            patch_preferences = intent.extracted_request_patch.get("preferences") or []
            if patch_preferences and user_id:
                merged_user_context = memory_manager.build_user_context(planning_request, user_id=user_id)
                memory_manager.persist_user_memory(user_id, merged_user_context)
            # 改稿也落行程记忆（与状态提交解耦，尽力而为）：让历史行程持续累积，
            # 避免 revise 后会话里只剩最新摘要、更早的规划信息无法追溯
            memory_manager.persist_trip_memory(
                user_id,
                planning_request,
                extract_plan_attractions(revise_result.artifacts.plan),
                list(planning_request.avoid_spots or []),
                summary,
                response_mode="revise_plan",
            )
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
            # qa / unknown：正常走 LLM 自由对话，带行程摘要上下文
            # LLM 不可用/失败时给友好引导，不把调试用 reasoning 暴露给用户
            summary = self._qa_chat_reply(agent_request.message, session_state) or (
                "你好！我是旅行规划助手，可以帮你规划旅行行程。"
                "告诉我目的地、游玩日期和人数，我就能开始为你规划。"
            )
        session_state = session_state_service.append_recent_message(session_state, "assistant", summary)
        self._save_or_log(session_state, request_id=agent_request.request_id, trace_id=trace_id)
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

    # 载入会话 → 填充行程摘要 → 意图识别 → 状态合并 → 乐观保存（快路径，可重试）。
    # 并发安全：repository.save 版本不一致时返回 None（说明其他请求已推进会话），
    # 这里基于最新状态重新计算并重试；最近消息只在意图识别后追加（纯历史），重试天然幂等。
    def _resolve_intent(
        self,
        *,
        session_id: str,
        user_id: str | None,
        request_id: str,
        raw_message: str,
        max_attempts: int = 3,
    ) -> tuple[SessionState, SessionIntentResult, dict, list, tuple | None]:
        # 空输入防护：不识别不调 LLM，直接 unknown 兜底（与 pipeline.run 口径一致；
        # 否则 build_intent_input 会对空 raw_message 触发 IntentRecognitionInput 校验异常）
        if not (raw_message or "").strip():
            session_state = redis_session_repository.load(session_id) or session_state_service.initialize(session_id, user_id)
            empty_intent = intent_session_pipeline.adapt_output_only({"intent_type": "unknown", "reasoning": "空输入。"})
            session_state = session_state_service.apply_intent_result(session_state, empty_intent).session_state
            session_state = session_state_service.append_recent_message(session_state, "user", raw_message)
            saved = redis_session_repository.save(session_state)
            return (saved or session_state), empty_intent, {}, [], None
        user_context: dict = {}
        prefetched_artifacts: tuple | None = None
        intent: SessionIntentResult | None = None
        last_intent_input = None
        for attempt in range(max_attempts):
            session_state = redis_session_repository.load(session_id) or session_state_service.initialize(session_id, user_id)

            # 填充已有行程摘要（latest_plan_summary）：
            # 意图识别（尤其 LLM 路径）靠它区分 new_plan（首次规划）vs revise_plan（改稿），
            # 若 SessionState 里还没有摘要，则从 Redis artifacts 的 plan 生成一份轻量摘要，
            # 并顺带预读产物供 revise 分支复用，避免同请求内重复读 Redis
            if session_state.artifacts.plan_summary is None:
                stored_plan, stored_draft = redis_session_repository.load_artifacts(session_id)
                if stored_plan:
                    session_state.artifacts.plan_summary = build_plan_summary(stored_plan)
                    # 自愈：产物在但标记缺失（旧数据/异常场景），补齐 has_plan 让
                    # repository.load 的产物续期与 revise 分支判定同时生效
                    session_state.artifacts.has_plan = True
                    if session_state.artifacts.plan_updated_at is None:
                        session_state.artifacts.plan_updated_at = utc_now()
                prefetched_artifacts = (stored_plan, stored_draft)

            # user_context 每次迭代都基于最新会话重建（从 memory + 当前累计需求推断），
            # 重试时并发请求可能已改偏好，旧 user_context 会让意图判定失真
            _req_for_ctx = session_state_to_planning_request(session_state)
            user_context = memory_manager.build_user_context(_req_for_ctx, user_id=user_id).model_dump()
            trip_history = memory_manager.load_trip_history(user_id)

            # 意图上下文指纹：重建 intent 输入做值比较。冲突重试时若上下文已变化
            # （如并发请求刚完成规划，has_plan/plan_summary/近期消息变化），复用旧意图会把
            # 改稿当重新规划、确认当闲聊等路由到错误分支，必须重跑识别；上下文未变才复用
            intent_input = intent_session_pipeline.build_intent_input(
                session_state=session_state,
                request_id=request_id,
                raw_message=raw_message,
                user_context=user_context,
                trip_history=trip_history,
            )
            if attempt == 0 or intent_input != last_intent_input:
                last_intent_input = intent_input
                result = intent_session_pipeline.run(
                    session_state=session_state,
                    request_id=request_id,
                    raw_message=raw_message,
                    user_context=user_context,
                    trip_history=trip_history,
                )
                session_state = result.session_state
                intent = result.intent_result
            else:
                # 上下文未变：复用首次识别结果，仅重放 merge+apply（避免重复调 LLM）
                assert intent is not None  # 首次迭代必已赋值
                session_state = session_state_service.apply_intent_result(session_state, intent).session_state

            # 记录用户本轮消息（意图识别之后追加，recent_messages 只含历史；重试不会重复记录）
            session_state = session_state_service.append_recent_message(session_state, "user", raw_message)

            saved = redis_session_repository.save(session_state)
            if saved is not None:
                # 返回升版后的 state，后续分支的二次保存以此为基底，不再误判冲突
                return saved, intent, user_context, trip_history, prefetched_artifacts
            app_logger.warning(
                "session_save_conflict",
                request_id=request_id,
                session_id=session_id,
                attempt=attempt + 1,
            )

        # 重试耗尽：放弃持久化本轮合并（其他请求已领先，避免无限重试）。
        # 最后一次迭代的状态已基于最新会话计算，响应仍有效，仅本轮状态更新不落库。
        app_logger.error("session_save_conflict_exhausted", request_id=request_id, session_id=session_id)
        return session_state, intent, user_context, trip_history, prefetched_artifacts

    # 保存会话状态；乐观并发冲突（其他请求已推进）时仅告警，不阻塞本次响应
    def _save_or_log(self, session_state: SessionState, *, request_id: str, trace_id: str) -> None:
        if redis_session_repository.save(session_state) is None:
            app_logger.warning(
                "session_save_conflict_dropped",
                request_id=request_id,
                trace_id=trace_id,
                session_id=session_state.session_id,
            )

    @staticmethod
    def _has_revision_signal(message: str) -> bool:
        """判断原话是否包含具体的改稿指令。

        策略：
        1. 空泛纯动词短语（"改一下"/"修改"/"调整下"）→ 无具体指令，返回 False
        2. 含具体修改动词 → 有指令，返回 True
        3. 无动词但消息较长（描述性表达，如"我想住得离景点近一点"）→ 视为有指令
        """
        stripped = message.strip()
        if _EMPTY_REVISE_PATTERN.fullmatch(stripped):
            return False
        if any(keyword in stripped for keyword in _REVISION_SIGNAL_KEYWORDS):
            return True
        return len(stripped) >= 5

    def _commit_artifacts_with_retry(
        self,
        session_state: SessionState,
        plan_payload: dict | None,
        draft_payload: dict | None,
        summary: str,
        max_attempts: int = 3,
    ) -> tuple[bool, SessionState]:
        """带冲突重试的产物提交：save_with_artifacts 冲突时以最新会话为基底重放本轮变更。

        返回 (是否成功, 最终 session_state)。
        重试幂等：每次基于最新版本重放"产物标记 + stage 推进 + assistant 消息"，
        不会重复追加消息；产物与摘要仍保持原子成对，不产生"新 plan + 旧摘要"错位。
        """
        current = session_state
        for _ in range(max_attempts):
            saved = redis_session_repository.save_with_artifacts(current, plan_payload, draft_payload)
            if saved is not None:
                return True, saved
            # 冲突：其他请求已推进该会话。以最新状态为基底，重放本轮产物与阶段变更后重试
            latest = redis_session_repository.load(current.session_id)
            if latest is None:
                return False, current
            current = latest.model_copy(deep=True)
            current.artifacts = session_state.artifacts.model_copy(deep=True)
            current.conversation_stage = "completed"
            current = session_state_service.append_recent_message(current, "assistant", summary)
        return False, current

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
            "recent_messages": [m.model_dump() for m in session_state.recent_messages[-6:]],
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
