from __future__ import annotations

from datetime import date, timedelta

from app.agents.prompt.planning import PLAN_CLUSTER_PROMPT, PLAN_RENDER_PROMPT, PLAN_SKELETON_PROMPT
from app.agents.schema.planning import (
    CandidateCluster,
    ClusterPlanInput,
    ClusterPlanning,
    FinalItineraryRenderInput,
    LodgingAnchorDecision,
    LodgingFitnessResult,
    PlanInput,
    PlanningBudgets,
    PlanningNextAction,
    PlanningRequest,
    PlanningSkeleton,
    SkeletonPlanInput,
    TransportCheckRequest,
    TripPlan,
)
from app.agents.sparse.planning import build_cluster_plan_prompt, build_itinerary_render_prompt, build_skeleton_prompt
from app.domain.context.builder import context_builder
from app.domain.context.planning import PlanningContext
from app.infrastructure.llm.client import get_llm_client
from app.infrastructure.llm.schemas import ItineraryDraftSchema
from app.domain.memory.manager import memory_manager
from app.tools.attraction import attraction_tool
from app.tools.lodging import lodging_tool
from app.tools.schema.attraction import AttractionInput
from app.tools.schema.lodging import LodgingInput
from app.tools.transport import transport_tool
from app.tools.weather import weather_tool


class PlanningAgent:
    def __init__(self) -> None:
        self._cluster_plan_prompt = PLAN_CLUSTER_PROMPT
        self._skeleton_prompt = PLAN_SKELETON_PROMPT
        self._render_prompt = PLAN_RENDER_PROMPT

    def run_pipeline(self, plan_input: PlanInput) -> dict:
        from app.agents.orchestrator import planning_orchestrator

        request = self._normalize_request(plan_input.request)
        state = context_builder.build_planning_context(request, user_id=plan_input.user_id, session_id=plan_input.session_id)
        # 只读字段校验兜底（对话阶段判定已在 orchestrator 侧由 SessionStateService 完成）
        dialogue_decision, response_context = planning_orchestrator.resolve(request, state.session)
        if dialogue_decision.status != "ready_to_plan":
            raise ValueError(dialogue_decision.follow_up_question or "缺少必要规划字段")

        state.trace.append({"step": "dialogue_ready", "status": dialogue_decision.status, "response_mode": response_context.response_mode})

        observation_log = self._run_planner_loop(state)
        if not state.attraction_result:
            attraction_observation = self._execute_tool_step(state, "attraction")
            observation_log.append(attraction_observation)
            state.trace.append({"step": "tool_attraction", **attraction_observation})
        if not state.weather_result:
            weather_observation = self._execute_tool_step(state, "weather")
            observation_log.append(weather_observation)
            state.trace.append({"step": "tool_weather", **weather_observation})
        if not state.lodging_result:
            lodging_observation = self._execute_tool_step(state, "lodging")
            observation_log.append(lodging_observation)
            state.trace.append({"step": "tool_lodging", **lodging_observation})
        budgets = PlanningBudgets()
        cluster_plan = self._build_cluster_plan(state, observation_log)
        if self._should_refresh_attractions(cluster_plan, budgets):
            self._refresh_attractions(state, cluster_plan)
            budgets.attraction_refresh_remaining -= 1
            observation_log.append(
                {
                    "step": "attraction_refresh",
                    "candidate_count": len(state.attraction_result.candidates) if state.attraction_result else 0,
                    "reason": cluster_plan.attraction_refresh_reason,
                }
            )
            cluster_plan = self._build_cluster_plan(state, observation_log)

        skeleton = self._build_skeleton_from_clusters(state, cluster_plan, observation_log, budgets)
        lodging_fitness = self._evaluate_lodging_fitness(state, skeleton)
        if self._should_refresh_lodging(lodging_fitness, budgets):
            self._refresh_lodging(state, lodging_fitness)
            budgets.lodging_refresh_remaining -= 1
            observation_log.append(
                {
                    "step": "lodging_refresh",
                    "selected_lodging": state.selected_lodging.name if state.selected_lodging else None,
                    "reason": lodging_fitness.reason,
                }
            )
            skeleton = self._build_skeleton_from_clusters(state, cluster_plan, observation_log, budgets)

        transport_request = self._extract_transport_check_request(skeleton)
        if self._should_fetch_transport(transport_request, budgets):
            self._fetch_targeted_transport(state, transport_request)
            budgets.transport_batch_remaining -= 1
            observation_log.append(
                {
                    "step": "transport_refresh",
                    "route_count": len(state.transport_results),
                    "targets": [target.model_dump() for target in transport_request.targets],
                }
            )

        lodging_anchor = self._resolve_lodging_anchor(state, skeleton)
        draft = self._render_final_itinerary(state, skeleton, lodging_anchor)
        draft = self._light_validate_and_repair(state, draft, skeleton, lodging_anchor, budgets)

        state.draft = draft
        state.trace.append({"step": "plan_agent", "day_count": len(draft.day_plans)})
        state.plan = self._compose_plan(state, skeleton, lodging_anchor)
        state.status = "completed"

        if not response_context.include_alternatives:
            state.plan.alternatives = []
        if not response_context.include_weather_notes:
            state.plan.weather_notes = []
        if not response_context.include_transport_plan:
            state.plan.transport_plan = []
        if not response_context.include_stay_recommendation:
            state.plan.stay_recommendation = []
        if not response_context.include_daily_plan:
            state.plan.daily_plan = []
        if not response_context.include_summary:
            state.plan.summary = ""

        memory_manager.persist_user_memory(plan_input.user_id, state.user)
        memory_manager.persist_trip_memory(
            plan_input.user_id,
            request,
            [item.name for item in (state.attraction_result.candidates if state.attraction_result else [])],
            list(request.avoid_spots or []),
            state.plan.summary if state.plan else draft.summary,
            response_mode=response_context.response_mode,
        )

        final_state = {
            "status": state.status,
            "request": request.model_dump(),
            "user_context": state.user.model_dump(),
            "session_context": state.session.model_dump(),
            "attraction_candidates": [item.model_dump() for item in (state.attraction_result.candidates if state.attraction_result else [])],
            "lodging_candidates": [item.model_dump() for item in (state.lodging_result.candidates if state.lodging_result else [])],
            "selected_lodging": state.selected_lodging.model_dump() if state.selected_lodging else None,
            "transport_result_count": len(state.transport_results),
            "weather_daily": [item.model_dump() for item in (state.weather_result.daily if state.weather_result else [])],
            "planning_skeleton": skeleton.model_dump(),
            "lodging_anchor": lodging_anchor.model_dump(),
        }

        final_decision = {
            "status": state.status,
            "summary": state.plan.summary if state.plan else draft.summary,
        }

        return {
            "planning_trace": state.trace,
            "final_state": final_state,
            "final_decision": final_decision,
            "final_draft": draft,
            "plan": state.plan,
        }

    def _run_planner_loop(self, state: PlanningContext) -> list[dict]:
        observation_log: list[dict] = []
        collected_tools: list[str] = []

        required_tools = ["attraction", "weather"]
        if self._should_collect_lodging(state):
            required_tools.append("lodging")

        for tool_name in required_tools:
            next_action = self._fallback_next_action(collected_tools)
            observation_log.append(
                {
                    "step": "planner_next_action",
                    "status": next_action.status,
                    "next_tool": tool_name,
                    "reason": next_action.reason,
                    "missing_information": next_action.missing_information,
                }
            )
            tool_observation = self._execute_tool_step(state, tool_name)
            observation_log.append(tool_observation)
            state.trace.append({"step": f"tool_{tool_name}", **tool_observation})
            collected_tools.append(tool_name)

        finish_reason = "已按默认顺序获取 attraction、weather 及必要的 lodging 信息，transport 延后到 skeleton 后按需补充。"
        observation_log.append(
            {
                "step": "planner_next_action",
                "status": "enough_to_plan",
                "next_tool": "none",
                "reason": finish_reason,
                "missing_information": [],
            }
        )
        state.trace.append({"step": "planner_loop_finish", "reason": finish_reason})
        return observation_log

    def _fallback_next_action(self, collected_tools: list[str]) -> PlanningNextAction:
        if "attraction" not in collected_tools:
            return PlanningNextAction(
                status="need_tool",
                next_tool="attraction",
                reason="需要候选景点素材来启动规划。",
                missing_information=["attraction_candidates"],
            )
        if "weather" not in collected_tools:
            return PlanningNextAction(
                status="need_tool",
                next_tool="weather",
                reason="多日旅行规划需要天气证据来约束户外/室内安排。",
                missing_information=["weather"],
            )
        if "lodging" not in collected_tools:
            return PlanningNextAction(
                status="need_tool",
                next_tool="lodging",
                reason="需要住宿候选作为参考，但不一定直接固定为行程锚点。",
                missing_information=["lodging_candidates"],
            )
        return PlanningNextAction(
            status="enough_to_plan",
            next_tool="none",
            reason="已有基础景点、天气和住宿参考，可先形成规划骨架。",
            missing_information=[],
        )

    def _build_cluster_plan(self, state: PlanningContext, observation_log: list[dict]) -> ClusterPlanning:
        llm_client = get_llm_client()
        payload = ClusterPlanInput(
            request=state.request.model_dump(),
            attraction_candidates=[item.model_dump() for item in (state.attraction_result.candidates if state.attraction_result else [])],
            lodging_candidates=[item.model_dump() for item in (state.lodging_result.candidates if state.lodging_result else [])],
            selected_lodging=state.selected_lodging.model_dump() if state.selected_lodging else None,
            weather=[item.model_dump() for item in (state.weather_result.daily if state.weather_result else [])],
            transport_evidence=[],
            observation_log=observation_log,
        )
        prompt = build_cluster_plan_prompt(payload)
        cluster_plan = llm_client.generate_cluster_plan(system_prompt=self._cluster_plan_prompt, user_prompt=prompt)
        if not cluster_plan:
            raise RuntimeError(f"Failed to generate cluster plan from LLM. Debug: {llm_client.last_debug_info}")
        return self._normalize_cluster_plan(state, cluster_plan)

    def _should_refresh_attractions(self, cluster_plan: ClusterPlanning, budgets: PlanningBudgets) -> bool:
        return bool(cluster_plan.needs_attraction_refresh and budgets.attraction_refresh_remaining > 0)

    def _refresh_attractions(self, state: PlanningContext, cluster_plan: ClusterPlanning) -> None:
        state.attraction_result = self._run_attractions(request=state.request, refill=True)
        state.trace.append(
            {
                "step": "tool_attraction_refresh",
                "candidate_count": len(state.attraction_result.candidates) if state.attraction_result else 0,
                "reason": cluster_plan.attraction_refresh_reason,
            }
        )

    def _build_skeleton_from_clusters(
        self,
        state: PlanningContext,
        cluster_plan: ClusterPlanning,
        observation_log: list[dict],
        budgets: PlanningBudgets,
    ) -> PlanningSkeleton:
        llm_client = get_llm_client()
        payload = SkeletonPlanInput(
            request=state.request.model_dump(),
            cluster_plans=cluster_plan.model_dump(),
            attraction_candidates=[item.model_dump() for item in (state.attraction_result.candidates if state.attraction_result else [])],
            lodging_candidates=[item.model_dump() for item in (state.lodging_result.candidates if state.lodging_result else [])],
            selected_lodging=state.selected_lodging.model_dump() if state.selected_lodging else None,
            selected_lodging_status="provisional" if state.selected_lodging else "stay_unconfirmed",
            weather=[item.model_dump() for item in (state.weather_result.daily if state.weather_result else [])],
            transport_evidence=[item.model_dump() for item in state.transport_results],
            planning_budgets=budgets.model_dump(),
            observation_log=observation_log,
        )
        prompt = build_skeleton_prompt(payload)
        skeleton = llm_client.generate_planning_skeleton(system_prompt=self._skeleton_prompt, user_prompt=prompt)
        if not skeleton:
            fallback = self._build_fallback_skeleton_from_clusters(state, cluster_plan)
            if fallback:
                state.trace.append({"step": "skeleton_fallback", "reason": "llm_parse_failed"})
                skeleton = fallback
            else:
                raise RuntimeError(f"Failed to generate planning skeleton from LLM. Debug: {llm_client.last_debug_info}")
        if not self._is_skeleton_within_candidates(state, skeleton):
            raise RuntimeError("Planning skeleton contains selected spots outside attraction candidates.")
        return self._normalize_skeleton_consistency(skeleton)

    def _evaluate_lodging_fitness(
        self,
        state: PlanningContext,
        skeleton: PlanningSkeleton,
    ) -> LodgingFitnessResult:
        if not state.selected_lodging:
            return LodgingFitnessResult(anchor_status="invalid", reason="当前没有可信的临时住宿锚点。")

        selected_spot_names = {spot for day in skeleton.day_skeletons for spot in day.selected_spots}
        selected_candidates = [
            item for item in (state.attraction_result.candidates if state.attraction_result else []) if item.name in selected_spot_names
        ]
        candidate_areas = {item.area for item in selected_candidates if item.area}
        lodging_area = state.selected_lodging.area
        if not self._is_displayable_lodging(state.selected_lodging.name):
            return LodgingFitnessResult(
                anchor_status="invalid",
                reason="当前住宿结果疑似非真实住宿实体。",
                recommended_area_hint="市区核心区",
            )
        if lodging_area and candidate_areas and lodging_area not in candidate_areas and len(candidate_areas) >= 2:
            return LodgingFitnessResult(
                anchor_status="needs_refresh",
                reason="当前住宿区域与多数天主簇明显错位，可能拉低整体动线效率。",
                recommended_area_hint=next(iter(candidate_areas)),
                suggested_reanchor_strategy="改选更贴近多数日主簇的住宿区域",
            )
        if skeleton.needs_lodging_refresh:
            return LodgingFitnessResult(
                anchor_status="needs_refresh",
                reason=skeleton.lodging_refresh_reason or "当前 skeleton 已明确提示住宿锚点可能不适配。",
                recommended_area_hint=lodging_area,
            )
        return LodgingFitnessResult(anchor_status="validated", reason="当前住宿锚点与 skeleton 未见明显结构冲突。")

    def _should_refresh_lodging(self, fitness: LodgingFitnessResult, budgets: PlanningBudgets) -> bool:
        return fitness.anchor_status in {"needs_refresh", "invalid"} and budgets.lodging_refresh_remaining > 0

    def _refresh_lodging(self, state: PlanningContext, fitness: LodgingFitnessResult) -> None:
        state.lodging_result = self._run_lodging(state.request, state)
        state.selected_lodging = state.lodging_result.selected_lodging if state.lodging_result else None
        state.trace.append(
            {
                "step": "tool_lodging_refresh",
                "candidate_count": len(state.lodging_result.candidates) if state.lodging_result else 0,
                "selected_lodging": state.selected_lodging.name if state.selected_lodging else None,
                "reason": fitness.reason,
            }
        )

    def _extract_transport_check_request(self, skeleton: PlanningSkeleton) -> TransportCheckRequest:
        if skeleton.transport_check_request:
            return skeleton.transport_check_request
        targets = []
        for day in skeleton.day_skeletons:
            targets.extend(day.transport_check_targets)
        return TransportCheckRequest(
            needs_transport_evidence=bool(skeleton.needs_transport_evidence or targets),
            targets=targets,
        )

    def _should_fetch_transport(self, request: TransportCheckRequest, budgets: PlanningBudgets) -> bool:
        return bool(request.needs_transport_evidence and request.targets and budgets.transport_batch_remaining > 0)

    def _fetch_targeted_transport(self, state: PlanningContext, request: TransportCheckRequest) -> None:
        routes = []
        for target in request.targets[:3]:
            routes.append(transport_tool.run(city=state.request.destination, from_name=target.from_label, to_name=target.to_label))
        state.transport_results = routes
        state.trace.append(
            {
                "step": "tool_transport",
                "route_count": len(state.transport_results),
                "reason": "skeleton 显示存在关键转场或远郊可行性判断需求，补充 targeted transport 证据。",
            }
        )

    def _resolve_lodging_anchor(self, state: PlanningContext, skeleton: PlanningSkeleton) -> LodgingAnchorDecision:
        selected = state.selected_lodging
        if not selected:
            return LodgingAnchorDecision(
                anchor_mode="stay_unconfirmed",
                anchor_name="住宿待定",
                anchor_area=None,
                reason="当前没有可信的住宿结果，暂不固定全程住宿锚点。",
            )

        if not self._is_displayable_lodging(selected.name):
            return LodgingAnchorDecision(
                anchor_mode="stay_unconfirmed",
                anchor_name="住宿待定",
                anchor_area=None,
                reason="当前住宿结果疑似非真实住宿实体，不作为主行程锚点。",
            )

        selected_spot_names = {spot for day in skeleton.day_skeletons for spot in day.selected_spots}
        remote_areas = {"延庆区", "怀柔区", "密云区", "平谷区"}
        remote_spot_selected = any(
            candidate.name in selected_spot_names and candidate.area in remote_areas
            for candidate in (state.attraction_result.candidates if state.attraction_result else [])
        )
        if selected.area in remote_areas and not remote_spot_selected:
            return LodgingAnchorDecision(
                anchor_mode="use_city_center_placeholder",
                anchor_name="市区住宿待定",
                anchor_area="市区核心区",
                reason="当前选中住宿位于远郊，但 skeleton 未显示多数天适合围绕该住宿展开，降级为市区住宿占位锚点。",
            )

        return LodgingAnchorDecision(
            anchor_mode="use_selected_lodging",
            anchor_name=selected.name,
            anchor_area=selected.area,
            reason="当前住宿结果看起来可信，且未发现明显与 skeleton 冲突，允许作为行程锚点。",
        )

    def _render_final_itinerary(
        self,
        state: PlanningContext,
        skeleton: PlanningSkeleton,
        lodging_anchor: LodgingAnchorDecision,
    ) -> ItineraryDraftSchema:
        llm_client = get_llm_client()
        payload = FinalItineraryRenderInput(
            request=state.request.model_dump(),
            skeleton=skeleton.model_dump(),
            weather=[item.model_dump() for item in (state.weather_result.daily if state.weather_result else [])],
            attraction_candidates=[item.model_dump() for item in (state.attraction_result.candidates if state.attraction_result else [])],
            lodging_candidates=[item.model_dump() for item in (state.lodging_result.candidates if state.lodging_result else [])],
            selected_lodging=state.selected_lodging.model_dump() if state.selected_lodging else None,
            planning_anchor=lodging_anchor.model_dump(),
            transport_evidence=[item.model_dump() for item in state.transport_results],
        )
        prompt = build_itinerary_render_prompt(payload)

        draft = llm_client.generate_itinerary_draft(system_prompt=self._render_prompt, user_prompt=prompt)
        if draft:
            return draft

        retry_prompt = prompt + "\n\n[render_retry_instruction]\n请缩短 detail 与 notes，优先保证输出完整、合法、可解析的 JSON。若不确定，请使用更短的 detail，但不要省略晚餐与晚间正式时段，也不要把一天在18:30–19:30就提前收掉。"
        draft = llm_client.generate_itinerary_draft(system_prompt=self._render_prompt, user_prompt=retry_prompt)
        if draft:
            return draft

        raise RuntimeError(f"Failed to render itinerary draft from LLM. Debug: {llm_client.last_debug_info}")

    def _light_validate_and_repair(
        self,
        state: PlanningContext,
        draft: ItineraryDraftSchema,
        skeleton: PlanningSkeleton,
        lodging_anchor: LodgingAnchorDecision,
        budgets: PlanningBudgets,
    ) -> ItineraryDraftSchema:
        if not draft.day_plans:
            raise RuntimeError("LLM returned empty itinerary draft.")

        draft.day_plans = sorted(draft.day_plans, key=lambda item: item.day_index)
        for day in draft.day_plans:
            cleaned_blocks = []
            previous_end = None
            for block in sorted(day.time_blocks, key=lambda item: item.start_time):
                if block.start_time >= block.end_time:
                    continue
                if previous_end and block.start_time < previous_end:
                    continue
                cleaned_blocks.append(block)
                previous_end = block.end_time
            day.time_blocks = cleaned_blocks
            day.notes = self._normalize_day_notes(day.notes)
            for block in day.time_blocks:
                if block.item_type == "attraction":
                    block.detail = self._normalize_candidate_detail(block.detail, block.area)

        if state.request.must_visit_spots:
            all_titles = {block.title for day in draft.day_plans for block in day.time_blocks if block.item_type == "attraction"}
            if not all(any(must_visit in title or title in must_visit for title in all_titles) for must_visit in state.request.must_visit_spots):
                raise RuntimeError("LLM draft is missing required must-visit spots.")

        if any(
            block.item_type == "attraction" and block.start_time >= "21:30"
            for day in draft.day_plans
            for block in day.time_blocks
        ):
            raise RuntimeError("LLM draft contains attraction blocks scheduled too late.")

        short_days = self._find_short_day_indices(draft)
        if short_days and budgets.render_repair_remaining > 0 and self._needs_llm_repair(draft, short_days):
            repaired = self._repair_invalid_draft(state, skeleton, lodging_anchor, short_days)
            budgets.render_repair_remaining -= 1
            if repaired:
                repaired.day_plans = sorted(repaired.day_plans, key=lambda item: item.day_index)
                for day in repaired.day_plans:
                    cleaned_blocks = []
                    previous_end = None
                    for block in sorted(day.time_blocks, key=lambda item: item.start_time):
                        if block.start_time >= block.end_time:
                            continue
                        if previous_end and block.start_time < previous_end:
                            continue
                        cleaned_blocks.append(block)
                        previous_end = block.end_time
                    day.time_blocks = cleaned_blocks
                    day.notes = self._normalize_day_notes(day.notes)
                    for block in day.time_blocks:
                        if block.item_type == "attraction":
                            block.detail = self._normalize_candidate_detail(block.detail, block.area)
                if not self._find_short_day_indices(repaired):
                    draft = repaired

        return draft


    def _compose_plan(self, state: PlanningContext, skeleton: PlanningSkeleton, lodging_anchor: LodgingAnchorDecision) -> TripPlan:
        draft = state.draft
        request = state.request
        lodging_candidates = state.lodging_result.candidates if state.lodging_result else []
        weather_days = state.weather_result.daily if state.weather_result else []

        daily_plan = []
        for day in draft.day_plans:
            daily_plan.append(
                {
                    "day_index": day.day_index,
                    "primary_area": day.primary_area,
                    "items": [
                        {
                            "title": self._normalize_block_title(block.title, block.item_type),
                            "start_time": block.start_time,
                            "end_time": block.end_time,
                            "area": block.area,
                            "detail": self._normalize_candidate_detail(block.detail, block.area),
                            "item_type": block.item_type,
                        }
                        for block in day.time_blocks
                    ],
                    "notes": self._normalize_day_notes(day.notes),
                }
            )

        stay_recommendation = []
        if lodging_anchor.anchor_mode == "use_selected_lodging" and state.selected_lodging:
            stay_recommendation.append(
                {
                    "name": state.selected_lodging.name,
                    "area": state.selected_lodging.area,
                    "selected": True,
                    "booking_note": state.selected_lodging.booking_note,
                }
            )
        else:
            for hotel in lodging_candidates[:3]:
                if self._is_displayable_lodging(hotel.name):
                    stay_recommendation.append({"name": hotel.name, "area": hotel.area})

        weather_notes = [f"{item.date}: {item.weather} {item.temperature_range or ''}".strip() for item in weather_days]
        transport_plan = self._build_transport_summary(draft)

        return TripPlan(
            destination=request.destination,
            summary=draft.summary if draft.summary else skeleton.summary,
            route_intent_summary=draft.route_intent_summary if draft.route_intent_summary else skeleton.overall_rationale,
            daily_plan=daily_plan,
            stay_recommendation=stay_recommendation,
            transport_plan=transport_plan,
            weather_notes=weather_notes,
            alternatives=list(skeleton.rejected_spots_global[:5]),
            reflection=None,
        )

    def _build_transport_summary(self, draft: ItineraryDraftSchema) -> list[dict]:
        summary = []
        for day in draft.day_plans:
            transitions = []
            blocks = day.time_blocks
            for idx in range(len(blocks) - 1):
                current_block = blocks[idx]
                next_block = blocks[idx + 1]
                if current_block.item_type in {"return", "transport"}:
                    continue
                transitions.append(
                    {
                        "from": current_block.title,
                        "to": next_block.title,
                        "advice": "参考当日明细中的转场安排。",
                    }
                )
            if transitions:
                summary.append({"day_index": day.day_index, "transitions": transitions[:3]})
        return summary

    def _execute_tool_step(self, state: PlanningContext, tool_name: str) -> dict:
        request = state.request
        if tool_name == "weather":
            state.weather_result = self._run_weather(request)
            return {
                "tool": "weather",
                "days": len(state.weather_result.daily) if state.weather_result else 0,
                "has_error": bool(state.weather_result.error) if state.weather_result else True,
            }
        if tool_name == "attraction":
            state.attraction_result = self._run_attractions(request)
            return {
                "tool": "attraction",
                "candidate_count": len(state.attraction_result.candidates) if state.attraction_result else 0,
                "must_visit_verified": len(state.attraction_result.must_visit_verified) if state.attraction_result else 0,
            }
        if tool_name == "lodging":
            state.lodging_result = self._run_lodging(request, state)
            state.selected_lodging = state.lodging_result.selected_lodging if state.lodging_result else None
            return {
                "tool": "lodging",
                "candidate_count": len(state.lodging_result.candidates) if state.lodging_result else 0,
                "selected_lodging": state.selected_lodging.name if state.selected_lodging else None,
            }
        if tool_name == "transport":
            return {
                "tool": "transport",
                "route_count": len(state.transport_results),
                "status": "deferred_until_skeleton",
            }
        return {"tool": tool_name, "status": "skipped"}

    def _run_weather(self, request: PlanningRequest):
        start_date, end_date = self._resolve_dates(request)
        return weather_tool.run(city=request.destination, start_time=start_date, end_time=end_date)

    def _run_attractions(self, request: PlanningRequest, refill: bool = False):
        return attraction_tool.run(
            AttractionInput(
                city=request.destination,
                days=request.days,
                must_visit_spots=request.must_visit_spots,
                avoid_spots=request.avoid_spots,
                preferences=request.preferences + request.optional_spots,
                target_count_min=10 if refill else 8,
                target_count_max=15 if refill else 12,
            )
        )

    def _run_lodging(self, request: PlanningRequest, state: PlanningContext):
        candidates = state.attraction_result.candidates if state.attraction_result else []
        core_spots = [item.name for item in candidates if item.entity_level == "main"][:4]
        support_spots = [item.name for item in candidates if item.entity_level != "main"][:2]
        spots = core_spots + [item for item in support_spots if item not in core_spots]
        preferences = list(request.preferences) + ["优先标准酒店", "适合作为全程锚点"]
        return lodging_tool.run(
            LodgingInput(
                destination=request.destination,
                budget=request.budget,
                preferences=preferences,
                avoid_spots=list(request.avoid_spots) + ["民宿", "旅馆招待所"],
                spots=spots,
            )
        )

    def _resolve_dates(self, request: PlanningRequest) -> tuple[str, str]:
        if request.start_date and request.end_date:
            return request.start_date, request.end_date
        start = date.today() + timedelta(days=7)
        end = start + timedelta(days=max(request.days - 1, 0))
        return str(start), str(end)

    def _normalize_request(self, request: PlanningRequest) -> PlanningRequest:
        return PlanningRequest(
            destination=str(request.destination).strip(),
            days=max(1, int(request.days or 1)),
            budget=request.budget,
            start_date=request.start_date,
            end_date=request.end_date,
            departure_city=request.departure_city,
            revision_message=str(request.revision_message).strip() if request.revision_message else None,
            travelers=list(request.travelers or []),
            preferences=list(request.preferences or []),
            must_visit_spots=list(request.must_visit_spots or []),
            optional_spots=list(request.optional_spots or []),
            avoid_spots=list(request.avoid_spots or []),
        )

    def _find_short_day_indices(self, draft: ItineraryDraftSchema) -> list[int]:
        short_days: list[int] = []
        for day in draft.day_plans:
            return_end = None
            evening_meal_exists = False
            evening_block_exists = False
            last_non_return_end = None

            for block in day.time_blocks:
                if block.item_type == "return":
                    return_end = block.end_time
                    continue
                last_non_return_end = block.end_time
                if block.item_type == "meal" and block.start_time >= "17:00":
                    evening_meal_exists = True
                if block.item_type in {"flex", "attraction", "meal"} and block.start_time >= "18:30":
                    evening_block_exists = True

            if return_end and return_end < "20:30":
                short_days.append(day.day_index)
                continue
            if last_non_return_end and last_non_return_end < "20:00":
                short_days.append(day.day_index)
                continue
            if not evening_meal_exists or not evening_block_exists:
                short_days.append(day.day_index)

        return short_days

    def _repair_invalid_draft(
        self,
        state: PlanningContext,
        skeleton: PlanningSkeleton,
        lodging_anchor: LodgingAnchorDecision,
        short_days: list[int],
    ) -> ItineraryDraftSchema | None:
        llm_client = get_llm_client()
        payload = FinalItineraryRenderInput(
            request=state.request.model_dump(),
            skeleton=skeleton.model_dump(),
            weather=[item.model_dump() for item in (state.weather_result.daily if state.weather_result else [])],
            attraction_candidates=[item.model_dump() for item in (state.attraction_result.candidates if state.attraction_result else [])],
            lodging_candidates=[item.model_dump() for item in (state.lodging_result.candidates if state.lodging_result else [])],
            selected_lodging=state.selected_lodging.model_dump() if state.selected_lodging else None,
            planning_anchor=lodging_anchor.model_dump(),
            transport_evidence=[item.model_dump() for item in state.transport_results],
        )
        prompt = build_itinerary_render_prompt(payload)
        retry_prompt = (
            prompt
            + "\n\n[short_day_retry]\n"
            + f"以下天数结束过早或缺失正式晚间时段：{short_days}。"
            + "请在不改变 skeleton 主选点决策的前提下，把这些天重新渲染成完整可用旅行日：显式补足晚餐、晚间正式活动/夜游/夜景/茶馆/商圈收尾，并把返程默认放在20:30之后、最好接近21:00–22:00；"
            + "不要制造长时间空窗，也不要只补一个很短的夜间块。"
        )
        return llm_client.generate_itinerary_draft(system_prompt=self._render_prompt, user_prompt=retry_prompt)

    def _needs_llm_repair(self, draft: ItineraryDraftSchema, short_days: list[int]) -> bool:
        for day in draft.day_plans:
            if day.day_index not in short_days:
                continue
            attraction_count = sum(1 for block in day.time_blocks if block.item_type == "attraction")
            meal_count = sum(1 for block in day.time_blocks if block.item_type == "meal")
            has_evening_block = any(block.start_time >= "18:30" for block in day.time_blocks if block.item_type in {"attraction", "meal", "flex", "return"})
            if len(day.time_blocks) <= 4 or attraction_count == 0 or meal_count < 2 or not has_evening_block:
                return True
        return False

    def _should_collect_lodging(self, state: PlanningContext) -> bool:
        return state.request.days >= 2

    def _normalize_block_title(self, title: str, item_type: str) -> str:
        title = title.strip()
        if item_type == "transport" and not title.startswith(("前往", "步行至", "乘", "打车至", "地铁至", "返回")):
            return f"前往{title}"
        if item_type == "meal" and not title.startswith(("午餐", "晚餐", "早餐")):
            return f"午餐：{title}"
        if item_type == "return" and not title.startswith("返回"):
            return f"返回{title}"
        if item_type == "flex" and not title.startswith("弹性"):
            return f"弹性：{title}"
        return title

    def _is_skeleton_within_candidates(self, state: PlanningContext, skeleton: PlanningSkeleton) -> bool:
        candidate_names = {item.name for item in (state.attraction_result.candidates if state.attraction_result else [])}
        selected_names = set(skeleton.selected_spots_global or [])
        selected_names.update(spot for day in skeleton.day_skeletons for spot in day.selected_spots)
        return selected_names.issubset(candidate_names)

    def _normalize_cluster_plan(self, state: PlanningContext, cluster_plan: ClusterPlanning) -> ClusterPlanning:
        candidate_names = {item.name for item in (state.attraction_result.candidates if state.attraction_result else [])}
        seen_selected: set[str] = set()
        normalized_clusters = []
        for index, cluster in enumerate(cluster_plan.clusters, start=1):
            cluster.cluster_id = cluster.cluster_id or f"cluster_{index}"
            cluster.selected_spots = [spot for spot in cluster.selected_spots if spot in candidate_names and spot not in seen_selected]
            seen_selected.update(cluster.selected_spots)
            cluster.optional_spots = [spot for spot in cluster.optional_spots if spot in candidate_names and spot not in cluster.selected_spots]
            cluster.rejected_spots = [spot for spot in cluster.rejected_spots if spot in candidate_names and spot not in cluster.selected_spots and spot not in cluster.optional_spots]
            if cluster.selected_spots:
                normalized_clusters.append(cluster)
        cluster_plan.clusters = normalized_clusters
        cluster_plan.rejected_spots_global = [spot for spot in cluster_plan.rejected_spots_global if spot in candidate_names and spot not in seen_selected]
        return cluster_plan

    def _build_fallback_skeleton_from_clusters(self, state: PlanningContext, cluster_plan: ClusterPlanning) -> PlanningSkeleton | None:
        total_days = max(1, state.request.days)
        ordered_clusters = [cluster for cluster in cluster_plan.clusters if cluster.selected_spots or cluster.optional_spots]

        if not ordered_clusters:
            candidates = list(state.attraction_result.candidates or []) if state.attraction_result else []
            if not candidates:
                return None
            grouped = []
            seen_areas: set[str] = set()
            for item in candidates:
                area = item.area or "未分区"
                if area in seen_areas:
                    continue
                seen_areas.add(area)
                grouped.append(
                    CandidateCluster(
                        cluster_id=f"fallback_{len(grouped) + 1}",
                        label=area,
                        selected_spots=[item.name],
                        optional_spots=[],
                        rejected_spots=[],
                        why_it_works=f"以{area}内的高价值候选先形成基础日骨架。",
                        weather_fit=None,
                        effort_level="balanced",
                        night_closure_style="晚餐后在主簇附近完成正式晚间收尾。",
                        must_stay_together=False,
                        is_remote_day_candidate=bool(getattr(item, "estimated_visit_duration_hours", 0) and getattr(item, "estimated_visit_duration_hours", 0) >= 4.5),
                    )
                )
                if len(grouped) >= total_days:
                    break
            if not grouped:
                return None
            ordered_clusters = grouped

        day_skeletons = []
        selected_global: list[str] = []
        for day_index in range(1, total_days + 1):
            cluster = ordered_clusters[min(day_index - 1, len(ordered_clusters) - 1)]
            base_selected = cluster.selected_spots or cluster.optional_spots
            selected_spots = base_selected[:2] if base_selected else []
            optional_spots = [spot for spot in cluster.optional_spots[:2] if spot not in selected_spots]
            selected_global.extend(spot for spot in selected_spots if spot not in selected_global)
            day_skeletons.append(
                {
                    "day_index": day_index,
                    "primary_cluster_id": cluster.cluster_id,
                    "selected_spots": selected_spots,
                    "optional_spots": optional_spots,
                    "rejected_spots": [spot for spot in cluster.rejected_spots if spot not in selected_spots and spot not in optional_spots],
                    "rationale": cluster.why_it_works or f"以{cluster.label}作为当天主线。",
                    "pacing": cluster.effort_level,
                    "weather_strategy": cluster.weather_fit,
                    "lunch_strategy": f"午餐围绕{cluster.label}主线顺路安排。",
                    "night_closure_strategy": cluster.night_closure_style or "晚餐后在主簇附近完成正式晚间收尾。",
                    "return_strategy": "默认在21:00–22:00左右完成返程闭合。",
                    "needs_transport_check": cluster.is_remote_day_candidate,
                    "transport_check_targets": [],
                    "needs_lodging_refresh_hint": False,
                }
            )
        return PlanningSkeleton.model_validate(
            {
                "destination": state.request.destination,
                "summary": cluster_plan.summary or f"{state.request.destination}{total_days}天行程骨架。",
                "overall_rationale": cluster_plan.overall_rationale,
                "selected_spots_global": selected_global,
                "rejected_spots_global": cluster_plan.rejected_spots_global,
                "day_skeletons": day_skeletons,
                "needs_transport_evidence": any(cluster.is_remote_day_candidate for cluster in ordered_clusters),
                "transport_check_request": {
                    "needs_transport_evidence": any(cluster.is_remote_day_candidate for cluster in ordered_clusters),
                    "targets": [],
                },
                "needs_lodging_refresh": False,
                "lodging_refresh_reason": None,
            }
        )

    def _normalize_skeleton_consistency(self, skeleton: PlanningSkeleton) -> PlanningSkeleton:
        normalized_day_selected: list[list[str]] = []
        global_selected_seen: set[str] = set()

        for day in skeleton.day_skeletons:
            unique_selected: list[str] = []
            day_seen: set[str] = set()
            for spot in day.selected_spots:
                if spot and spot not in day_seen:
                    day_seen.add(spot)
                    unique_selected.append(spot)
                    global_selected_seen.add(spot)
            day.selected_spots = unique_selected
            normalized_day_selected.append(unique_selected)

            unique_optional: list[str] = []
            optional_seen: set[str] = set()
            for spot in day.optional_spots:
                if not spot or spot in day_seen or spot in optional_seen:
                    continue
                optional_seen.add(spot)
                unique_optional.append(spot)
            day.optional_spots = unique_optional

            reject_seen: set[str] = set()
            unique_rejected: list[str] = []
            forbidden = set(day.selected_spots) | set(day.optional_spots)
            for spot in day.rejected_spots:
                if not spot or spot in forbidden or spot in reject_seen:
                    continue
                reject_seen.add(spot)
                unique_rejected.append(spot)
            day.rejected_spots = unique_rejected

        selected_union: list[str] = []
        selected_union_seen: set[str] = set()
        for spots in normalized_day_selected:
            for spot in spots:
                if spot not in selected_union_seen:
                    selected_union_seen.add(spot)
                    selected_union.append(spot)

        ordered_global_selected: list[str] = []
        for spot in skeleton.selected_spots_global:
            if spot in global_selected_seen and spot not in ordered_global_selected:
                ordered_global_selected.append(spot)
        for spot in selected_union:
            if spot not in ordered_global_selected:
                ordered_global_selected.append(spot)
        skeleton.selected_spots_global = ordered_global_selected

        selected_global_set = set(skeleton.selected_spots_global)
        global_reject_seen: set[str] = set()
        normalized_global_rejected: list[str] = []
        for spot in skeleton.rejected_spots_global:
            if not spot or spot in selected_global_set or spot in global_reject_seen:
                continue
            global_reject_seen.add(spot)
            normalized_global_rejected.append(spot)
        skeleton.rejected_spots_global = normalized_global_rejected
        return skeleton

    def _normalize_candidate_detail(self, detail: str | None, area: str | None) -> str | None:
        if not detail:
            return detail
        segments = [segment.strip() for segment in detail.split(";") if segment.strip()]
        cleaned: list[str] = []
        internal_markers = ("main", "independent", "optional", "sub")
        area_tokens = [token for token in [area] if token]

        for segment in segments:
            lowered = segment.lower()
            if lowered in internal_markers:
                continue
            if any(marker in segment for marker in ["非子项", "独立文保单位", "应保留", "作为主项", "必须保留"]):
                continue
            if area and any(token in segment for token in area_tokens if token != area) and area not in segment:
                continue
            cleaned.append(segment)

        if not cleaned:
            fallback = segments[-1] if segments else detail
            return fallback.strip()
        return "；".join(cleaned)

    def _normalize_day_notes(self, notes: list[str]) -> list[str]:
        if not notes:
            return []
        keep_keywords = [
            "预约",
            "天气",
            "高温",
            "雷阵雨",
            "中雨",
            "小雨",
            "风险",
            "门票",
            "闭馆",
            "注意",
            "携带",
            "入场",
            "开放",
            "限流",
            "排队",
            "避雨",
            "防晒",
            "换乘",
            "预警",
            "地铁",
            "打车",
        ]
        normalized = [note.strip() for note in notes if note and any(keyword in note for keyword in keep_keywords)]
        if not normalized:
            normalized = [note.strip() for note in notes if str(note).strip()]
        deduped: list[str] = []
        seen: set[str] = set()
        for note in normalized:
            if note and note not in seen:
                seen.add(note)
                deduped.append(note)
        return deduped[:4]

    def _is_displayable_lodging(self, name: str | None) -> bool:
        if not name:
            return False
        lowered = name.lower()
        lodging_keywords = ["酒店", "宾馆", "民宿", "客栈", "公寓", "hotel", "inn", "hostel", "residence", "stay", "apartment"]
        scenic_keywords = ["长城", "公园", "博物馆", "广场", "遗址", "景区", "胡同", "宫", "坛", "湖", "园", "街", "咖啡"]
        if any(keyword in lowered for keyword in lodging_keywords):
            return True
        if any(keyword in name for keyword in scenic_keywords):
            return False
        return True


planning_agent = PlanningAgent()
