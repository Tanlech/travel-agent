from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from uuid import uuid4

from app.agent.agents.planning import planning_agent
from app.agent.agents.prompt.orchestrator import ORCHESTRATOR_QA_CHAT_PROMPT
from app.agent.agents.revise import revise_agent
from app.agent.agents.schema.orchestrator import AgentRequest, AgentResponse
from app.agent.agents.schema.planning import PlanInput, PlanningRequest, TripPlan
from app.agent.agents.schema.revise import RevisionIntent, ReviseAgentInput, ReviseExecutionPolicy
from app.observability.token_budget import TokenBudgetTracker, default_budget_policy
from app.agent.domain.common.planning import compute_missing_fields, extract_plan_attractions
from app.agent.domain.common.time import utc_now
from app.agent.domain.common.user import UserContext
from app.agent.domain.memory.manager import memory_manager
from app.agent.domain.session.mapper import (
    build_follow_up_question,
    build_plan_summary,
    session_state_to_planning_request,
    session_state_to_session_context,
    stage_to_execution_mode,
)
from app.agent.domain.session.pipeline import intent_session_pipeline
from app.agent.domain.session.repository import redis_session_repository
from app.agent.domain.session.schema import SessionIntentResult, SessionState
from app.agent.domain.session.service import session_state_service
from app.agent.domain.common.itinerary import ItineraryDraftSchema
from app.agent.knowledge import ATTRACTION_COLLECTION, knowledge_service
from app.agent.knowledge.ingest.common import CHAT_COLLECTION, QA_COLLECTION
from app.agent.knowledge.schemas import RetrievalItem
from app.infrastructure.llm_client import get_llm_client
from app.observability.monitoring import app_logger, metrics_recorder
from app.observability.tracing import new_trace_id


# 空泛改稿预检：具体修改动词（命中即认为原话含修改指令）
_REVISION_SIGNAL_KEYWORDS = (
    "改", "换", "调整", "修改", "更改", "删", "去掉", "增加", "添加",
    "提前", "推迟", "延长", "缩短", "重新", "优化",
)

# 仅由"修改动词 + 可选语气词"构成、不含具体宾语的空泛改稿（如"改一下"/"修改"/"调整下"）
_EMPTY_REVISE_PATTERN = re.compile(
    r"^(?:修改|调整|改|换|优化|更改|变更|重排|重做)(?:一下|下|一改|一换|吧|呗|点|了|一遍)?$"
)

# qa/unknown 分支走 LLM 自由对话；LLM 不可用或失败时给用户的固定引导语
_DEFAULT_QA_FALLBACK = (
    "你好！我是旅行规划助手，可以帮你规划旅行行程。"
    "告诉我目的地、游玩日期和人数，我就能开始为你规划。"
)

# 分支注册表：ExecutionMode → 处理方法名。新增模式（如团游/多目的地）只需在此注册，
# handle 主体不做修改（未知模式统一走 qa 兜底）
_HANDLERS = {
    "clarify": "_handle_clarify",
    "planning": "_handle_planning",
    "revise": "_handle_revise",
    "qa": "_handle_qa",
}

# 会话阶段迁移表：orchestrator 只在规划/改稿成功后把阶段推进为 completed，
# 集中声明合法来源阶段，未来新增迁移（closed / 新一轮 clarify）只在此扩展；
# 异常迁移打点告警，避免加分支时漏迁移导致状态机漂移
_STAGE_TO_COMPLETED_SOURCES = frozenset(
    {"ready_to_plan", "revise_collecting", "revise_ready", "qa", "completed"}
)

# qa 分支的固定回复：confirm / reject / end_session 直接透传 reasoning 或此默认文案，不走 LLM
_QA_STATIC_REPLIES = {
    "confirm": "好的，行程已确认。",
    "reject": "好的，那就不规划了。有需要随时找我。",
    "end_session": "好的，祝你旅途愉快！再见。",
}


@dataclass
class _BranchCtx:
    """分支执行上下文：聚合各分支共用的输入，消除 handler 的重复长签名"""

    mode: str
    agent_request: AgentRequest
    session_state: SessionState
    intent: SessionIntentResult
    user_context: dict
    trip_history: list
    prefetched_artifacts: tuple | None
    trace_id: str
    budget_tracker: TokenBudgetTracker
    session_id: str


class TravelOrchestrator:
    """对话编排入口

    通过 IntentSessionPipeline 串联意图识别与会话状态，SessionState 落 Redis，
    按 conversation_stage 路由到 clarify / planning / revise / qa 四个分支
    """

    def handle(self, agent_request: AgentRequest) -> AgentResponse:
        trace_id = new_trace_id()
        budget_tracker = TokenBudgetTracker(default_budget_policy())
        session_id = agent_request.session_id or str(uuid4())

        # 请求级幂等：同一 request_id 二次投递（客户端/网关对同一请求重试）直接返回首次结果，
        # 避免重复规划 / 重复落记忆等副作用；缓存同时绑定 message，不同消息复用 request_id 不命中
        cached = self._load_cached_response(session_id, agent_request.request_id, agent_request.message)
        if cached is not None:
            app_logger.info("orchestrator_idempotent_hit", request_id=agent_request.request_id, trace_id=trace_id, mode=cached.mode)
            metrics_recorder.record("orchestrator_idempotent_hits", 1, mode=cached.mode)
            return cached

        # 载入会话 + 意图识别 + 状态合并 + 乐观保存（含并发冲突重试）
        session_state, intent, user_context, trip_history, prefetched_artifacts = self._resolve_intent(
            session_id=session_id,
            user_id=agent_request.user_id,
            request_id=agent_request.request_id,
            raw_message=agent_request.message,
            trace_id=trace_id,
        )

        mode = stage_to_execution_mode(session_state.conversation_stage)

        # 会话已关闭（closed 终态）：结束态拦截，不再走分支编排，明确引导开新会话。
        # 原本 closed 会经 stage_to_execution_mode 落进 qa 闲聊，用户消息被当成普通对话，
        # 无法感知"行程已结束"；此处显式返回引导，并缓存幂等结果
        if session_state.conversation_stage == "closed":
            message = "本次行程已结束。需要规划新的旅行，请开启新会话。"
            app_logger.info("orchestrator_closed_replay", request_id=agent_request.request_id, trace_id=trace_id)
            metrics_recorder.record("orchestrator_closed_replay", 1)
            response = AgentResponse(
                request_id=agent_request.request_id,
                session_id=session_id,
                status="completed",
                mode="closed",
                summary=message,
                trace_id=trace_id,
                metrics={"token_budget": budget_tracker.allocate(0), "remaining_budget": budget_tracker.remaining()},
                debug=self._debug_payload(intent, session_state, error="session_closed"),
            )
            self._cache_completed_response(session_id, agent_request.request_id, agent_request.message, response)
            return response

        app_logger.info("orchestrator_start", request_id=agent_request.request_id, trace_id=trace_id, mode=mode)
        metrics_recorder.record("orchestrator_requests", 1, mode=mode)

        ctx = _BranchCtx(
            mode=mode,
            agent_request=agent_request,
            session_state=session_state,
            intent=intent,
            user_context=user_context,
            trip_history=trip_history,
            prefetched_artifacts=prefetched_artifacts,
            trace_id=trace_id,
            budget_tracker=budget_tracker,
            session_id=session_id,
        )
        response = self._dispatch(ctx)
        # 仅缓存成功结果（副作用分支已完成落库），失败/追问不缓存
        self._cache_completed_response(session_id, agent_request.request_id, agent_request.message, response)
        return response

    # 分支分发：按注册表查表调用，统一注入上下文 + 记录分支耗时
    def _dispatch(self, ctx: _BranchCtx) -> AgentResponse:
        handler = getattr(self, _HANDLERS.get(ctx.mode, "_handle_qa"))
        start = time.perf_counter()
        response = handler(ctx)
        duration_ms = int((time.perf_counter() - start) * 1000)
        metrics_recorder.record("orchestrator_branch_duration_ms", duration_ms, mode=ctx.mode)
        # 关键阶段日志：每个分支完成一行，含模式、返回状态、耗时，便于链路回溯
        app_logger.info(
            "orchestrator_branch_done",
            mode=ctx.mode,
            status=response.status,
            duration_ms=duration_ms,
            request_id=ctx.agent_request.request_id,
            trace_id=ctx.trace_id,
        )
        # 回复内容摘要（前 140 字），让人一眼看到 agent 到底输出了什么
        reply_text = response.summary or ""
        app_logger.info(
            "orchestrator_reply",
            mode=ctx.mode,
            reply=f"{reply_text[:140]}{'…' if len(reply_text) > 140 else ''}",
            reply_len=len(reply_text),
        )
        return response

    # 请求级幂等：读取首次处理结果（Redis 短 TTL）。缓存绑定 message：同一 request_id
    # 携带不同消息（客户端误用）视为新请求不命中，避免返回陈旧响应
    def _load_cached_response(self, session_id: str, request_id: str, message: str) -> AgentResponse | None:
        payload = redis_session_repository.load_idempotent_response(session_id, request_id)
        if payload is None or payload.get("message") != message:
            return None
        try:
            return AgentResponse(**payload["response"])
        except Exception:
            return None

    # 请求级幂等：成功（completed）响应落 Redis（短 TTL），供同 request_id 重试直接复用；
    # message 一并存入，读取时校验内容一致
    def _cache_completed_response(self, session_id: str, request_id: str, message: str, response: AgentResponse) -> None:
        if response.status != "completed":
            return
        redis_session_repository.save_idempotent_response(
            session_id,
            request_id,
            {"message": message, "response": response.model_dump(mode="json")},
        )

    # 分支一：信息不齐，追问补全
    def _handle_clarify(self, ctx: _BranchCtx) -> AgentResponse:
        follow_up = build_follow_up_question(ctx.session_state.pending_questions)
        return self._notify_and_respond(
            ctx,
            follow_up,
            status="needs_follow_up",
            follow_up_question=follow_up,
            metrics={"token_budget": ctx.budget_tracker.allocate(300)},
        )

    # 分支二：执行旅行规划
    def _handle_planning(self, ctx: _BranchCtx) -> AgentResponse:
        planning_request = session_state_to_planning_request(ctx.session_state)
        try:
            plan_result = planning_agent.run_pipeline(
                PlanInput(request=planning_request, user_id=ctx.agent_request.user_id, session_id=ctx.session_id)
            )
        except Exception as exc:
            return self._fail_branch(ctx, error=str(exc), message="行程规划失败，请稍后重试或补充更多信息。")
        plan = plan_result.get("plan")
        draft = plan_result.get("final_draft")
        summary = (plan_result.get("final_decision") or {}).get("summary") or "已为你生成旅行行程。"
        if not plan:
            # 防御：run_pipeline 正常失败必抛异常走 except，此处仅覆盖"返回缺 plan 的 dict"这一不应发生
            # 的情形，按失败统一处理，避免"无行程却标记已完成 / 推进会话阶段"的语义矛盾
            return self._fail_branch(ctx, error="plan_missing_in_pipeline_result", message="行程规划失败，请稍后重试或补充更多信息。")
        return self._finalize_success(
            ctx,
            plan_payload=plan.model_dump(),
            draft_payload=draft.model_dump() if draft else None,
            summary=summary,
            token_budget=5000,
            fail_message="行程已生成但保存失败，请稍后重试。",
        )

    # 分支三：更新已有旅行（改稿）
    def _handle_revise(self, ctx: _BranchCtx) -> AgentResponse:
        # 复用 _resolve_intent 预读取的产物（仅在 plan_summary 缺失时才预读），避免同请求内重复读 Redis
        plan_payload, draft_payload = (
            ctx.prefetched_artifacts if ctx.prefetched_artifacts is not None else redis_session_repository.load_artifacts(ctx.session_id)
        )
        if not plan_payload or not draft_payload:
            # 区分"从未生成过行程"与"产物已过期丢失"，给出不同引导
            if ctx.session_state.artifacts.has_plan:
                msg = "之前的行程数据已过期，请让我重新为你生成一份。"
                # 产物已丢：连摘要一起清空，避免意图层仍凭 plan_summary 判定"已有行程"
                # 导致下一轮 revise 又走进这里，形成"反复提示已过期"的死循环
                ctx.session_state.artifacts.has_plan = False
                ctx.session_state.artifacts.plan_summary = None
                ctx.session_state.artifacts.plan_updated_at = None
            elif not compute_missing_fields(session_state_to_planning_request(ctx.session_state)):
                # 没有可改的行程：字段已齐（至少目的地）时直接降级为规划，重新生成行程，
                # 而不是让用户卡在"还没有行程"死胡同（用户反馈：没有就直接变成 plan 即可）
                app_logger.info(
                    "orchestrator_revise_fallback_to_planning",
                    request_id=ctx.agent_request.request_id,
                    trace_id=ctx.trace_id,
                )
                ctx.mode = "planning"
                return self._handle_planning(ctx)
            else:
                msg = "当前会话还没有可修改的行程，请先让我为你生成一份。"
            return self._notify_and_respond(ctx, msg, status="failed")

        planning_request = session_state_to_planning_request(ctx.session_state)
        session_context = session_state_to_session_context(ctx.session_state)

        # 空泛改稿预检：既没提取出字段修改，原话也不含具体修改指令（如"改一下"）。
        # 此时直接执行会产出无意义的整段重写，应追问具体要改哪部分
        if not ctx.intent.extracted_request_patch and not self._has_revision_signal(ctx.agent_request.message):
            msg = "请告诉我具体想调整哪里，比如换住宿、改景点或调整日期。"
            return self._notify_and_respond(
                ctx,
                msg,
                status="needs_follow_up",
                follow_up_question=msg,
                metrics={"token_budget": ctx.budget_tracker.allocate(300)},
            )

        revise_input = ReviseAgentInput(
            request=planning_request,
            user_context=UserContext(**ctx.user_context),
            session_context=session_context,
            execution_policy=ReviseExecutionPolicy(),
            current_plan=TripPlan(**plan_payload),
            current_draft=ItineraryDraftSchema(**draft_payload),
            revision_intent=RevisionIntent(
                user_message=ctx.agent_request.message,
                change_scope=ctx.intent.revision_scope_hint or "day_level",
                revision_goal=ctx.agent_request.message,
            ),
            bootstrap_intent={
                "change_scope": ctx.intent.revision_scope_hint,
                "revision_message": ctx.agent_request.message,
            },
            # 复用 _resolve_intent 已加载的 trip_history，避免同请求重复读 Redis
            trip_history=ctx.trip_history,
        )
        try:
            revise_result = revise_agent.run(revise_input)
        except Exception as exc:
            return self._fail_branch(ctx, error=str(exc), message="行程修改失败，请稍后重试或换个说法。")
        plan_model = revise_result.artifacts.plan
        draft_model = revise_result.artifacts.draft
        summary = revise_result.summary or "已按你的要求更新行程。"
        return self._finalize_success(
            ctx,
            plan_payload=plan_model.model_dump(),
            draft_payload=draft_model.model_dump(),
            summary=summary,
            token_budget=4000,
            fail_message="行程已修改但保存失败，请稍后重试。",
            # 提交成功后落记忆（与状态提交解耦，尽力而为，不阻断响应）
            on_success=lambda: self._persist_revision_memory(ctx, planning_request, plan_model, summary),
            debug_extra={"revision_summary": revise_result.revision_summary},
        )

    # 改稿成功后的记忆沉淀：偏好并入长期记忆 + 行程记忆累积（尽力而为，不阻断响应）
    def _persist_revision_memory(self, ctx: _BranchCtx, planning_request: PlanningRequest, plan_model: TripPlan, summary: str) -> None:
        # 改稿消息中新表达的偏好并入长期记忆（与规划分支 persist_user_memory 对齐）：
        # 改稿常伴随偏好微调（如"换成都市的酒店"），不能只存在于本轮、规划分支却能沉淀；
        # 基于 merge 后的完整 request 重建 user_context，布尔/否定偏好
        patch_preferences = ctx.intent.extracted_request_patch.get("preferences") or []
        if patch_preferences and ctx.agent_request.user_id:
            merged_user_context = memory_manager.build_user_context(planning_request, user_id=ctx.agent_request.user_id)
            memory_manager.persist_user_memory(ctx.agent_request.user_id, merged_user_context)
        # 改稿也落行程记忆：让历史行程持续累积，避免 revise 后会话里只剩最新摘要、更早的规划信息无法追溯
        memory_manager.persist_trip_memory(
            ctx.agent_request.user_id,
            planning_request,
            extract_plan_attractions(plan_model),
            list(planning_request.avoid_spots or []),
            summary,
            response_mode="revise_plan",
        )

    # 分支四：问答 / 确认 / 拒绝 / 结束
    def _handle_qa(self, ctx: _BranchCtx) -> AgentResponse:
        # confirm/reject/end_session 用固定文案（reasoning 是"为何分类"的推理、不展示给用户）；
        # qa/unknown 必须走 LLM 自由对话做真实回复：reasoning 绝不能当回答返回导致"念念推理过程"，
        # LLM 不可用/失败时才降级友好引导
        intent_type = ctx.intent.intent_type
        if intent_type in _QA_STATIC_REPLIES:
            summary = _QA_STATIC_REPLIES[intent_type]
        else:
            summary = self._qa_chat_reply(ctx.agent_request.message, ctx.session_state)
            if summary is None:
                summary = _DEFAULT_QA_FALLBACK
        return self._notify_and_respond(
            ctx,
            summary,
            status="completed",
            metrics={"token_budget": ctx.budget_tracker.allocate(500)},
        )

    # 规划/改稿成功收口：标记产物 → 推进阶段 → 追加消息 → 原子提交。
    # 提交耗尽降级 failed（响应语义与持久化一致）；成功后执行 on_success（如记忆沉淀）并返回 completed
    def _finalize_success(
        self,
        ctx: _BranchCtx,
        *,
        plan_payload: dict,
        draft_payload: dict | None,
        summary: str,
        token_budget: int,
        fail_message: str,
        on_success=None,
        debug_extra: dict | None = None,
    ) -> AgentResponse:
        # 写回会话产物（完整 plan/draft 与 state 原子提交，摘要存 SessionState）
        # 生成新摘要供下一轮意图识别使用（覆盖旧值）
        ctx.session_state.artifacts.plan_summary = build_plan_summary(plan_payload)
        # 产物标记：已成功生成，revise 分支据此区分"从未生成"与"产物丢失"
        ctx.session_state.artifacts.has_plan = True
        ctx.session_state.artifacts.plan_updated_at = utc_now()
        # 规划/改稿完成，推进阶段，避免后续闲聊(unknown)被误判成 ready_to_plan 触发重新规划
        ctx.session_state = self._advance_to_completed(ctx.session_state)
        ctx.session_state = session_state_service.append_recent_message(ctx.session_state, "assistant", summary)
        # 原子提交：state 与产物同事务，冲突整体回滚，避免"新 plan + 旧摘要"错位；
        # 冲突时以最新会话为基底重试，避免产物静默丢失导致下次请求重复执行
        saved, ctx.session_state = self._commit_artifacts_with_retry(ctx.session_state, plan_payload, draft_payload, summary)
        if not saved:
            # 提交冲突持续耗尽：产物与状态未落库，若仍返回 completed 会造成"响应声称成功
            # 但会话未推进/产物未保存"，下次请求重复执行。降级为 failed，响应语义与持久化一致
            return self._fail_branch(ctx, error="commit_conflict_exhausted", message=fail_message)
        if on_success is not None:
            on_success()
        return AgentResponse(
            request_id=ctx.agent_request.request_id,
            session_id=ctx.session_id,
            status="completed",
            mode=ctx.mode,
            summary=summary,
            plan=plan_payload,
            draft=draft_payload,
            trace_id=ctx.trace_id,
            metrics={"token_budget": ctx.budget_tracker.allocate(token_budget), "remaining_budget": ctx.budget_tracker.remaining()},
            debug=self._debug_payload(ctx.intent, ctx.session_state, **(debug_extra or {})),
        )

    # 载入会话 → 填充行程摘要 → 意图识别 → 状态合并 → 乐观保存（快路径，可重试）
    # 并发安全：repository.save 版本不一致时返回 None（说明其他请求已推进会话），
    # 这里基于最新状态重新计算并重试；最近消息只在意图识别后追加（纯历史），重试天然幂等
    def _resolve_intent(
        self,
        *,
        session_id: str,
        user_id: str | None,
        request_id: str,
        raw_message: str,
        trace_id: str,
        max_attempts: int = 3,
    ) -> tuple[SessionState, SessionIntentResult, dict, list, tuple | None]:
        # 空输入防护：不识别不调 LLM，直接 unknown 兜底（与 pipeline.run 口径一致；
        # 否则 build_intent_input 会对空 raw_message 触发 IntentRecognitionInput 校验异常）
        if not (raw_message or "").strip():
            session_state = self._load_or_init(session_id, user_id)
            empty_intent = intent_session_pipeline.adapt_output_only({"intent_type": "unknown", "reasoning": "空输入。"})
            session_state = session_state_service.apply_intent_result(session_state, empty_intent).session_state
            session_state = session_state_service.append_recent_message(session_state, "user", raw_message)
            # 复用统一保存告警：冲突时仅告警不阻塞，与主循环一致（空消息落库影响极小）
            self._save_or_log(session_state, request_id=request_id, trace_id=trace_id)
            return session_state, empty_intent, {}, [], None
        user_context: dict = {}
        prefetched_artifacts: tuple | None = None
        intent: SessionIntentResult | None = None
        last_intent_input = None
        for attempt in range(max_attempts):
            session_state = self._load_or_init(session_id, user_id)

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
            planning_request = session_state_to_planning_request(session_state)
            user_context = memory_manager.build_user_context(planning_request, user_id=user_id).model_dump()
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
                metrics_recorder.record("intent_recognitions", 1)
            else:
                # 上下文未变：复用首次识别结果，仅重放 merge+apply（避免重复调 LLM）
                assert intent is not None  # 首次迭代必已赋值
                session_state = session_state_service.apply_intent_result(session_state, intent).session_state
                metrics_recorder.record("intent_fingerprint_reuse", 1)

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
            metrics_recorder.record("session_save_conflicts", 1, phase="resolve_intent")

        # 重试耗尽：放弃持久化本轮合并（其他请求已领先，避免无限重试）
        # 最后一次迭代的状态已基于最新会话计算，响应仍有效，仅本轮状态更新不落库。
        # 循环首轮必已赋值 session_state 与 intent（attempt==0 恒走识别分支）
        app_logger.error("session_save_conflict_exhausted", request_id=request_id, session_id=session_id)
        metrics_recorder.record("session_save_exhausted", 1, phase="resolve_intent")
        return session_state, intent, user_context, trip_history, prefetched_artifacts

    # 载入会话；无会话时初始化（统一版本化写入基元，供各重试路径复用）
    def _load_or_init(self, session_id: str, user_id: str | None) -> SessionState:
        return redis_session_repository.load(session_id) or session_state_service.initialize(session_id, user_id)

    # 会话状态机：把阶段推进到 completed，按迁移表校验来源合法性；
    # 非法迁移打点告警（不影响执行），便于发现未来加分支时的漏迁移
    def _advance_to_completed(self, session_state: SessionState) -> SessionState:
        if session_state.conversation_stage not in _STAGE_TO_COMPLETED_SOURCES:
            app_logger.warning(
                "stage_transition_unexpected",
                from_stage=session_state.conversation_stage,
                to_stage="completed",
            )
            metrics_recorder.record(
                "stage_transition_unexpected",
                1,
                from_stage=session_state.conversation_stage,
            )
        return session_state.model_copy(update={"conversation_stage": "completed"})

    # 保存会话状态；乐观并发冲突（其他请求已推进）时仅告警，不阻塞本次响应
    def _save_or_log(self, session_state: SessionState, *, request_id: str, trace_id: str) -> None:
        saved = redis_session_repository.save(session_state) is not None
        self._warn_if_dropped(saved, request_id=request_id, trace_id=trace_id, session_id=session_state.session_id)

    # 追加 assistant 消息并保存会话（乐观冲突时仅告警，不阻塞本次响应）
    def _notify_and_save(self, session_state: SessionState, message: str, *, request_id: str, trace_id: str) -> SessionState:
        session_state = session_state_service.append_recent_message(session_state, "assistant", message)
        self._save_or_log(session_state, request_id=request_id, trace_id=trace_id)
        return session_state

    # 统一"追加 assistant 消息并保存会话 → 构造响应"路径：clarify/qa/revise 引导与失败兜底共用，
    # 消除各分支重复的 notify_and_save + AgentResponse 样板（mode 一律取 ctx.mode，与路由一致）
    def _notify_and_respond(
        self,
        ctx: _BranchCtx,
        message: str,
        *,
        status: str,
        follow_up_question: str | None = None,
        metrics: dict | None = None,
        debug_extra: dict | None = None,
    ) -> AgentResponse:
        ctx.session_state = self._notify_and_save(
            ctx.session_state, message, request_id=ctx.agent_request.request_id, trace_id=ctx.trace_id
        )
        return AgentResponse(
            request_id=ctx.agent_request.request_id,
            session_id=ctx.session_id,
            status=status,
            mode=ctx.mode,
            follow_up_question=follow_up_question,
            summary=message,
            trace_id=ctx.trace_id,
            metrics=metrics or {},
            debug=self._debug_payload(ctx.intent, ctx.session_state, **(debug_extra or {})),
        )

    # 保存失败（乐观冲突丢弃 / 产物提交失败）时统一告警
    # saved 为假说明本轮状态/产物未落库，不阻塞本次响应，但记录以便排查
    @staticmethod
    def _warn_if_dropped(saved: bool, *, request_id: str, trace_id: str, session_id: str) -> None:
        if not saved:
            app_logger.warning("session_save_conflict_dropped", request_id=request_id, trace_id=trace_id, session_id=session_id)
            metrics_recorder.record("session_save_dropped", 1)

    # 统一 debug 载荷：intent 全量 + 当前 stage + 分支附加信息
    @staticmethod
    def _debug_payload(intent: SessionIntentResult, session_state: SessionState, **extra) -> dict:
        payload = {"intent": intent.model_dump(), "stage": session_state.conversation_stage}
        payload.update(extra)
        return payload

    # 统一失败路径：记录日志 + 追加 assistant 消息 + 保存 + 返回 failed 响应
    def _fail_branch(self, ctx: _BranchCtx, *, error: str, message: str) -> AgentResponse:
        app_logger.error(f"{ctx.mode}_failed", request_id=ctx.agent_request.request_id, trace_id=ctx.trace_id, error=error)
        return self._notify_and_respond(ctx, message, status="failed", debug_extra={"error": error})

    @staticmethod
    def _has_revision_signal(message: str) -> bool:
        """判断原话是否包含具体的改稿指令"""
        stripped = message.strip()
        if _EMPTY_REVISE_PATTERN.fullmatch(stripped):
            return False
        return any(keyword in stripped for keyword in _REVISION_SIGNAL_KEYWORDS)

    def _commit_artifacts_with_retry(
        self,
        session_state: SessionState,
        plan_payload: dict | None,
        draft_payload: dict | None,
        summary: str,
        max_attempts: int = 3,
    ) -> tuple[bool, SessionState]:
        """带冲突重试的产物提交：save_with_artifacts 冲突时以最新会话为基底重放本轮变更"""
        current = session_state
        for _ in range(max_attempts):
            saved = redis_session_repository.save_with_artifacts(current, plan_payload, draft_payload)
            if saved is not None:
                return True, saved
            # 冲突：其他请求已推进该会话。以最新状态为基底，重放本轮产物与阶段变更后重试
            metrics_recorder.record("session_save_conflicts", 1, phase="commit_artifacts")
            latest = redis_session_repository.load(current.session_id)
            if latest is None:
                return False, current
            current = latest.model_copy(deep=True)
            current.artifacts = session_state.artifacts.model_copy(deep=True)
            current = self._advance_to_completed(current)
            current = session_state_service.append_recent_message(current, "assistant", summary)
        metrics_recorder.record("session_save_exhausted", 1, phase="commit_artifacts")
        return False, current

    # qa 分支的自由对话：用 LLM 认真回答用户问题
    # 输入：用户原话 + 当前会话状态（行程摘要/近期对话）
    # 输出：纯文本回复；LLM 不可用/失败时返回 None，由调用方降级
    # 关键：把近期对话作为真实 message 历史传入（而非塞进一段 JSON），user 原话单独成最后一条，
    # 这样 LLM 是正常连续聊天，不会"照着 JSON 里的 user_message 字段逐字回显"。
    def _qa_chat_reply(self, user_message: str, session_state: SessionState) -> str | None:
        llm_client = get_llm_client()
        if not llm_client.is_enabled():
            return None

        # system 提示词内附带当前行程摘要（若有），供 LLM 参考
        system_cue = ORCHESTRATOR_QA_CHAT_PROMPT
        plan_summary = session_state.artifacts.plan_summary or {}
        if plan_summary:
            system_cue += (
                "\n\n【当前行程上下文】\n"
                f"目的地：{plan_summary.get('destination') or '未定'}\n"
                f"天数：{plan_summary.get('days') or '未定'}\n"
                f"摘要：{(plan_summary.get('summary') or '')[:300]}"
            )

        # 近期对话轮次转成合法的 user/assistant 历史；最后一条若已是本轮 user 原话则移交给 user_prompt
        history: list[dict] = []
        for m in session_state.recent_messages[-6:]:
            role = "user" if m.role == "user" else "assistant"
            history.append({"role": role, "content": m.content or ""})
        if history and history[-1]["role"] == "user" and history[-1]["content"] == user_message:
            history.pop()

        # 对话问答时从知识库综合检索相关知识，作为参考资料注入，引导 AI 输出更好内容
        # （仅检索不回写；检索失败/为空时不影响本次自由回答）
        ref_context = self._retrieve_kb_context(user_message)
        user_prompt = f"【知识参考】\n{ref_context}\n\n{user_message}" if ref_context else user_message

        reply = llm_client.generate_chat_reply(
            system_prompt=system_cue,
            user_prompt=user_prompt,
            history=history,
        )
        return reply

    # 问答 RAG 检索：从问答库/景点库/对话库三处各取 top_k 相关块，轮转去重后拼成参考文本。
    # 三库相似度并不可比，用轮转混合保证来源多样，避免某一个库分数虚高独占上下文
    def _retrieve_kb_context(self, query: str, top_k: int = 2, max_total: int = 6) -> str:
        pools: dict[str, list[RetrievalItem]] = {}
        for coll in (QA_COLLECTION, ATTRACTION_COLLECTION, CHAT_COLLECTION):
            try:
                result = knowledge_service.retrieve(coll, query, top_k=top_k)
                pools[coll] = list(result.items)
            except Exception:
                app_logger.warning("kb_retrieve_fail", collection=coll, query=query)

        lines: list[str] = []
        seen: set[str] = set()
        # 轮转混合：依次从每个库吐出一条，未达到上限且有剩余时继续
        while sum(len(v) for v in pools.values()) and len(lines) < max_total:
            for coll, items in pools.items():
                if not items:
                    continue
                item = items.pop(0)
                if item.text in seen:
                    continue
                seen.add(item.text)
                meta = item.metadata or {}
                src = meta.get("source") or meta.get("city") or ""
                label = self._kb_label(coll)
                suffix = f"（{src}）" if src else ""
                lines.append(f"- [{label}]{suffix} {item.text}")
        return "\n".join(lines)

    @staticmethod
    def _kb_label(collection: str) -> str:
        labels = {
            QA_COLLECTION: "攻略",
            ATTRACTION_COLLECTION: "景点",
            CHAT_COLLECTION: "对话",
        }
        return labels.get(collection, collection)


travel_orchestrator = TravelOrchestrator()
