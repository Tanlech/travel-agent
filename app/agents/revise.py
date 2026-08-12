from __future__ import annotations

from datetime import date, timedelta

from app.agents.prompt.revise import REVISE_BLOCK_PROMPT, REVISE_DAY_PROMPT, REVISE_GLOBAL_PROMPT, REVISION_INTENT_PROMPT
from app.agents.reflection import planning_reflection
from app.agents.repair import planning_repair
from app.agents.schema.planning import PlanningRequest
from app.agents.schema.revise import (
    BlockLevelReviseResultSchema,
    DayLevelReviseResultSchema,
    ReviseAgentInput,
    ReviseAgentOutput,
    ReviseArtifacts,
    ReviseDebugTrace,
    RevisionImpactAnalysis,
    RevisionIntent,
)
from app.agents.sparse.revise import build_block_level_revise_prompt, build_day_level_revise_prompt, build_global_revise_prompt, build_revision_intent_prompt
from app.infrastructure.llm.client import get_llm_client
from app.infrastructure.llm.schemas import ItineraryDraftSchema
from app.tools.attraction import attraction_tool
from app.tools.lodging import lodging_tool
from app.tools.schema.attraction import AttractionInput
from app.tools.schema.lodging import LodgingInput
from app.tools.transport import transport_tool
from app.tools.weather import weather_tool


class ReviseAgent:
    """
    Revise agent skeleton.

    当前版本只完成：
    1. 输入接收与结构校验
    2. revise intent 影响范围分析
    3. 基于 scope 选择 revise 策略分支
    4. 返回与原 draft/plan 兼容的统一输出结构

    暂不执行真正的局部重排/重建 skeleton/重渲染，
    这些逻辑留给后续逐步补全。
    """

    def run(self, agent_input: ReviseAgentInput) -> ReviseAgentOutput:
        trace: list[ReviseDebugTrace] = []

        if agent_input.bootstrap_intent:
            trace.append(
                ReviseDebugTrace(
                    step="revise_bootstrap_intent",
                    status="ok",
                    payload=agent_input.bootstrap_intent,
                )
            )

        bootstrap_payload = agent_input.bootstrap_intent or {}
        intent = self._ensure_revision_intent(agent_input)
        trace.append(
            ReviseDebugTrace(
                step="revise_intent_normalized",
                status="ok",
                payload={
                    "normalized_intent": intent.model_dump(),
                    "diff_from_bootstrap": self._build_intent_diff(bootstrap_payload, intent.model_dump()),
                },
            )
        )
        trace.append(
            ReviseDebugTrace(
                step="revise_input_received",
                status="ok",
                payload={
                    "change_scope": intent.change_scope,
                    "affected_days": intent.affected_days,
                    "preserve_unchanged_days": intent.preserve_unchanged_days,
                },
            )
        )

        impact = self._analyze_impact(agent_input, intent)
        trace.append(
            ReviseDebugTrace(
                step="revise_impact_analysis",
                status="ok",
                payload=impact.model_dump(),
            )
        )

        intent_debug = {
            "bootstrap": agent_input.bootstrap_intent,
            "normalized": intent.model_dump(),
            "diff_from_bootstrap": self._build_intent_diff(agent_input.bootstrap_intent, intent.model_dump()) if agent_input.bootstrap_intent else {},
            "impact": impact.model_dump(),
        }

        refreshed_context = self._maybe_refresh_tools(agent_input, intent, impact, trace)
        revised_artifacts = self._run_revise_strategy(agent_input, intent, impact, refreshed_context, trace)

        pre_quality_artifacts = revised_artifacts
        output = ReviseAgentOutput(
            artifacts=revised_artifacts,
            revision_intent=intent,
            impact_analysis=impact,
            revision_trace=trace,
            revision_summary={
                "scope": impact.scope,
                "affected_days": impact.affected_days,
                "reused_days": impact.reused_days,
                "requires_tool_refresh": impact.requires_tool_refresh,
                "required_tools": impact.required_tools,
                "should_rebuild_skeleton": impact.should_rebuild_skeleton,
                "should_rerender_full_draft": impact.should_rerender_full_draft,
                "intent_debug": intent_debug,
            },
            status="completed",
            summary=self._build_summary(intent, impact),
        )

        output.revision_summary["pre_quality_draft"] = pre_quality_artifacts.draft.model_dump()
        return self._review_and_repair(
            agent_output=output,
            agent_input=agent_input,
        )

    def _ensure_revision_intent(self, agent_input: ReviseAgentInput) -> RevisionIntent:
        current = agent_input.revision_intent
        if self._is_revision_intent_usable(current):
            return current

        llm_client = get_llm_client()
        if not llm_client.is_enabled():
            return current

        user_prompt = build_revision_intent_prompt(
            request=agent_input.request,
            current_plan=agent_input.current_plan,
            current_draft=agent_input.current_draft,
            raw_revision_message=current.user_message,
            user_context=agent_input.user_context,
            session_context=agent_input.session_context,
        )
        return llm_client.generate_revision_intent(
            system_prompt=REVISION_INTENT_PROMPT,
            user_prompt=user_prompt,
        ) or current

    def _analyze_impact(self, agent_input: ReviseAgentInput, intent: RevisionIntent) -> RevisionImpactAnalysis:
        day_count = len(agent_input.current_draft.day_plans) if agent_input.current_draft else len(agent_input.current_plan.daily_plan)
        all_days = list(range(1, day_count + 1))

        affected_days = sorted(set(intent.affected_days))
        if intent.change_scope == "global":
            affected_days = all_days
        elif intent.change_scope == "block_level" and not affected_days:
            inferred_day = self._infer_single_day_from_blocks(intent)
            if inferred_day is not None:
                affected_days = [inferred_day]

        reused_days = [day for day in all_days if day not in affected_days] if intent.preserve_unchanged_days else []

        requires_tool_refresh = bool(
            intent.weather_replan or intent.transport_replan or intent.lodging_change or intent.added_spots
        )
        required_tools: list[str] = []
        if intent.weather_replan:
            required_tools.append("weather")
        if intent.transport_replan:
            required_tools.append("transport")
        if intent.lodging_change:
            required_tools.append("lodging")
        if intent.added_spots:
            required_tools.append("attraction")

        should_rebuild_skeleton = intent.change_scope in {"day_level", "global"}
        should_rerender_full_draft = bool(intent.change_scope == "global" and not intent.preserve_unchanged_days)

        return RevisionImpactAnalysis(
            scope=intent.change_scope,
            affected_days=affected_days,
            affected_block_ids=intent.affected_block_ids,
            reused_days=reused_days,
            locked_days=intent.locked_days,
            requires_tool_refresh=requires_tool_refresh,
            required_tools=required_tools,
            should_rebuild_skeleton=should_rebuild_skeleton,
            should_rerender_full_draft=should_rerender_full_draft,
            reason=self._build_impact_reason(intent, affected_days, requires_tool_refresh),
        )

    def _run_revise_strategy(
        self,
        agent_input: ReviseAgentInput,
        intent: RevisionIntent,
        impact: RevisionImpactAnalysis,
        refreshed_context: dict,
        trace: list[ReviseDebugTrace],
    ) -> ReviseArtifacts:
        if impact.scope == "block_level":
            return self._block_level_revise(agent_input, intent, impact, refreshed_context, trace)
        if impact.scope == "day_level":
            return self._day_level_revise(agent_input, intent, impact, refreshed_context, trace)
        return self._global_revise(agent_input, intent, impact, refreshed_context, trace)

    def _block_level_revise(
        self,
        agent_input: ReviseAgentInput,
        intent: RevisionIntent,
        impact: RevisionImpactAnalysis,
        refreshed_context: dict,
        trace: list[ReviseDebugTrace],
    ) -> ReviseArtifacts:
        trace.append(
            ReviseDebugTrace(
                step="revise_strategy_selected",
                status="block_level",
                message="进入 block-level revise。",
                payload={
                    "affected_days": impact.affected_days,
                    "affected_block_ids": impact.affected_block_ids,
                },
            )
        )

        current_draft = agent_input.current_draft or self._recover_draft(agent_input)
        llm_client = get_llm_client()
        if not llm_client.is_enabled():
            return ReviseArtifacts(draft=current_draft, plan=agent_input.current_plan)

        user_prompt = build_block_level_revise_prompt(
            request=agent_input.request,
            current_plan=agent_input.current_plan,
            current_draft=current_draft,
            revision_intent=intent,
            impact=impact,
            refreshed_context=refreshed_context,
        )
        revised_patch = llm_client.generate_block_level_revise(
            system_prompt=REVISE_BLOCK_PROMPT,
            user_prompt=user_prompt,
        )
        if revised_patch is None or not revised_patch.day_plans:
            trace.append(
                ReviseDebugTrace(
                    step="block_level_revise_llm",
                    status="fallback_original",
                    message="LLM block-level revise 未返回有效结果，回退原 draft。",
                )
            )
            return ReviseArtifacts(draft=current_draft, plan=agent_input.current_plan)

        new_draft = self._replace_day_plans(current_draft, revised_patch)
        trace.append(
            ReviseDebugTrace(
                step="block_level_revise_llm",
                status="ok",
                payload={
                    "affected_days": revised_patch.affected_days,
                    "changed_blocks_summary": revised_patch.changed_blocks_summary,
                    "revised_summary_fragment": revised_patch.revised_summary_fragment,
                },
            )
        )
        new_plan = self._render_trip_plan_from_draft(new_draft, agent_input)
        return ReviseArtifacts(draft=new_draft, plan=new_plan)

    def _day_level_revise(
        self,
        agent_input: ReviseAgentInput,
        intent: RevisionIntent,
        impact: RevisionImpactAnalysis,
        refreshed_context: dict,
        trace: list[ReviseDebugTrace],
    ) -> ReviseArtifacts:
        trace.append(
            ReviseDebugTrace(
                step="revise_strategy_selected",
                status="day_level",
                message="进入 day-level revise。",
                payload={
                    "affected_days": impact.affected_days,
                    "requires_tool_refresh": impact.requires_tool_refresh,
                    "required_tools": impact.required_tools,
                },
            )
        )

        current_draft = agent_input.current_draft or self._recover_draft(agent_input)
        llm_client = get_llm_client()
        if not llm_client.is_enabled():
            return ReviseArtifacts(draft=current_draft, plan=agent_input.current_plan)

        user_prompt = build_day_level_revise_prompt(
            request=agent_input.request,
            current_plan=agent_input.current_plan,
            current_draft=current_draft,
            revision_intent=intent,
            impact=impact,
            refreshed_context=refreshed_context,
        )
        revised_days = llm_client.generate_day_level_revise(
            system_prompt=REVISE_DAY_PROMPT,
            user_prompt=user_prompt,
        )
        if revised_days is None or not revised_days.day_plans:
            trace.append(
                ReviseDebugTrace(
                    step="day_level_revise_llm",
                    status="fallback_original",
                    message="LLM day-level revise 未返回有效结果，回退原 draft。",
                )
            )
            return ReviseArtifacts(draft=current_draft, plan=agent_input.current_plan)

        new_draft = self._replace_day_plans(current_draft, revised_days)
        trace.append(
            ReviseDebugTrace(
                step="day_level_revise_llm",
                status="ok",
                payload={
                    "affected_days": revised_days.affected_days,
                    "revised_summary_fragment": revised_days.revised_summary_fragment,
                },
            )
        )
        new_plan = self._render_trip_plan_from_draft(new_draft, agent_input)
        return ReviseArtifacts(draft=new_draft, plan=new_plan)

    def _global_revise(
        self,
        agent_input: ReviseAgentInput,
        intent: RevisionIntent,
        impact: RevisionImpactAnalysis,
        refreshed_context: dict,
        trace: list[ReviseDebugTrace],
    ) -> ReviseArtifacts:
        trace.append(
            ReviseDebugTrace(
                step="revise_strategy_selected",
                status="global",
                message="进入 global revise。",
                payload={
                    "affected_days": impact.affected_days,
                    "should_rebuild_skeleton": impact.should_rebuild_skeleton,
                    "should_rerender_full_draft": impact.should_rerender_full_draft,
                },
            )
        )

        current_draft = agent_input.current_draft or self._recover_draft(agent_input)
        llm_client = get_llm_client()
        if not llm_client.is_enabled():
            return ReviseArtifacts(draft=current_draft, plan=agent_input.current_plan)

        user_prompt = build_global_revise_prompt(
            request=agent_input.request,
            current_plan=agent_input.current_plan,
            current_draft=current_draft,
            revision_intent=intent,
            impact=impact,
            refreshed_context=refreshed_context,
        )
        revised_draft = llm_client.generate_global_revise(
            system_prompt=REVISE_GLOBAL_PROMPT,
            user_prompt=user_prompt,
        )
        if revised_draft is None or not revised_draft.day_plans:
            trace.append(
                ReviseDebugTrace(
                    step="global_revise_llm",
                    status="fallback_original",
                    message="LLM global revise 未返回有效结果，回退原 draft。",
                )
            )
            return ReviseArtifacts(draft=current_draft, plan=agent_input.current_plan)

        trace.append(
            ReviseDebugTrace(
                step="global_revise_llm",
                status="ok",
                payload={
                    "day_count": len(revised_draft.day_plans),
                    "selected_day_areas": list(revised_draft.selected_day_areas),
                },
            )
        )
        new_plan = self._render_trip_plan_from_draft(revised_draft, agent_input)
        return ReviseArtifacts(draft=revised_draft, plan=new_plan)

    def _replace_day_plans(
        self,
        original_draft: ItineraryDraftSchema,
        revised_days: BlockLevelReviseResultSchema | DayLevelReviseResultSchema,
    ) -> ItineraryDraftSchema:
        updated_by_day = {day.day_index: day for day in revised_days.day_plans}
        merged_day_plans = []
        for day in original_draft.day_plans:
            merged_day_plans.append(updated_by_day.get(day.day_index, day))

        summary = original_draft.summary
        if revised_days.revised_summary_fragment:
            summary = revised_days.revised_summary_fragment

        return ItineraryDraftSchema(
            destination=original_draft.destination,
            summary=summary,
            route_intent_summary=original_draft.route_intent_summary,
            selected_day_areas=[day.primary_area for day in merged_day_plans if day.primary_area],
            day_plans=merged_day_plans,
        )

    def _render_trip_plan_from_draft(self, draft: ItineraryDraftSchema, agent_input: ReviseAgentInput):
        daily_plan = []
        for day in draft.day_plans:
            daily_plan.append(
                {
                    "day_index": day.day_index,
                    "primary_area": day.primary_area,
                    "items": [
                        {
                            "title": block.title.strip(),
                            "start_time": block.start_time,
                            "end_time": block.end_time,
                            "area": block.area,
                            "detail": block.detail,
                            "item_type": block.item_type,
                        }
                        for block in day.time_blocks
                    ],
                    "notes": list(day.notes or []),
                }
            )

        transport_plan = []
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
                        "recommended_mode": "步行/地铁/打车",
                    }
                )
            transport_plan.append(
                {
                    "day_index": day.day_index,
                    "primary_area": day.primary_area,
                    "transitions": transitions,
                }
            )

        return agent_input.current_plan.__class__(
            destination=draft.destination or agent_input.current_plan.destination,
            summary=draft.summary or agent_input.current_plan.summary,
            route_intent_summary=draft.route_intent_summary or agent_input.current_plan.route_intent_summary,
            daily_plan=daily_plan,
            stay_recommendation=list(agent_input.current_plan.stay_recommendation or []),
            transport_plan=transport_plan,
            weather_notes=list(agent_input.current_plan.weather_notes or []),
            alternatives=list(agent_input.current_plan.alternatives or []),
            reflection=agent_input.current_plan.reflection,
        )

    def _review_and_repair(self, *, agent_output: ReviseAgentOutput, agent_input: ReviseAgentInput) -> ReviseAgentOutput:
        from app.domain.context.planning import PlanningContext
        from app.domain.context.session import SessionContext
        from app.domain.context.user import UserContext

        state = PlanningContext(
            request=agent_input.request,
            user=UserContext(
                preferred_styles=list(agent_input.user_context.preferred_styles or []),
                disliked_styles=list(agent_input.user_context.disliked_styles or []),
                accept_theme_park=agent_input.user_context.accept_theme_park,
                accept_nightlife=agent_input.user_context.accept_nightlife,
                pace_preference=agent_input.user_context.pace_preference,
                family_friendly=agent_input.user_context.family_friendly,
                senior_friendly=agent_input.user_context.senior_friendly,
            ),
            session=SessionContext(
                session_id=agent_input.session_context.session_id,
                confirmed_fields=list(agent_input.session_context.confirmed_fields),
                pending_questions=list(agent_input.session_context.pending_questions),
                conversation_stage=agent_input.session_context.conversation_stage,
                last_destination=agent_input.session_context.last_destination,
                revision_count=agent_input.session_context.revision_count,
            ),
            draft=agent_output.artifacts.draft,
            plan=agent_output.artifacts.plan,
            trace=[item.model_dump() for item in agent_output.revision_trace],
            revision_count=getattr(agent_input.session_context, "revision_count", 0),
        )
        state.reflection_result = planning_reflection.review(state)
        state.trace.append(
            {
                "step": "revise_reflection",
                "status": state.reflection_result.status,
                "issue_count": len(state.reflection_result.issues),
            }
        )
        if state.reflection_result.status == "revise":
            state = planning_repair.repair(state)
            state.plan = self._render_trip_plan_from_draft(state.draft, agent_input)
            state.trace.append({"step": "revise_repair_applied", "revision_count": state.revision_count})

        return ReviseAgentOutput(
            artifacts=agent_output.artifacts.__class__(draft=state.draft, plan=state.plan),
            revision_intent=agent_output.revision_intent,
            impact_analysis=agent_output.impact_analysis,
            revision_trace=agent_output.revision_trace.__class__(
                [
                    *agent_output.revision_trace,
                    *[
                        ReviseDebugTrace(
                            step=item.get("step", "revise_followup"),
                            status=item.get("status") if isinstance(item.get("status"), str) else None,
                            message=item.get("message") if isinstance(item.get("message"), str) else None,
                            payload=item if isinstance(item, dict) else {},
                        )
                        for item in state.trace[len(agent_output.revision_trace) :]
                    ],
                ]
            ),
            revision_summary={
                **agent_output.revision_summary,
                "reflection_status": state.reflection_result.status if state.reflection_result else None,
                "reflection_issue_count": len(state.reflection_result.issues) if state.reflection_result else 0,
                "repair_applied": bool(state.reflection_result and state.reflection_result.status == "revise"),
            },
            status=agent_output.status,
            summary=agent_output.summary,
        )

    def _recover_draft(self, _agent_input: ReviseAgentInput):
        raise ValueError("ReviseAgent requires current_draft for this skeleton version.")

    def _is_revision_intent_usable(self, intent: RevisionIntent) -> bool:
        return bool(intent.user_message and intent.change_scope and intent.revision_goal)

    def _infer_single_day_from_blocks(self, intent: RevisionIntent) -> int | None:
        for block_id in intent.affected_block_ids:
            if not isinstance(block_id, str):
                continue
            lowered = block_id.lower()
            if lowered.startswith("day"):
                digits = "".join(ch for ch in lowered if ch.isdigit())
                if digits:
                    return int(digits)
        return None

    def _build_intent_diff(self, bootstrap_payload: dict, normalized_payload: dict) -> dict:
        diff: dict[str, dict[str, object]] = {}
        keys = set(bootstrap_payload.keys()) | set(normalized_payload.keys())
        for key in sorted(keys):
            before = bootstrap_payload.get(key)
            after = normalized_payload.get(key)
            if before != after:
                diff[key] = {"before": before, "after": after}
        return diff

    def _maybe_refresh_tools(
        self,
        agent_input: ReviseAgentInput,
        intent: RevisionIntent,
        impact: RevisionImpactAnalysis,
        trace: list[ReviseDebugTrace],
    ) -> dict:
        refreshed_context = {"weather": None, "attraction": None, "lodging": None, "transport": None}
        if not impact.requires_tool_refresh or not agent_input.execution_policy.allow_tool_refresh:
            trace.append(
                ReviseDebugTrace(
                    step="revise_tool_refresh",
                    status="skipped",
                    payload={"required_tools": impact.required_tools},
                )
            )
            return refreshed_context

        if "weather" in impact.required_tools:
            refreshed_context["weather"] = self._refresh_weather(agent_input.request)
        if "attraction" in impact.required_tools:
            refreshed_context["attraction"] = self._refresh_attractions(agent_input, intent)
        if "lodging" in impact.required_tools:
            refreshed_context["lodging"] = self._refresh_lodging(agent_input)
        if "transport" in impact.required_tools:
            refreshed_context["transport"] = self._refresh_transport(agent_input, impact)

        trace.append(
            ReviseDebugTrace(
                step="revise_tool_refresh",
                status="ok",
                payload={
                    "required_tools": impact.required_tools,
                    "available": [key for key, value in refreshed_context.items() if value is not None],
                },
            )
        )
        return refreshed_context

    def _refresh_weather(self, request: PlanningRequest):
        start_date, end_date = self._resolve_dates(request)
        return weather_tool.run(city=request.destination, start_time=start_date, end_time=end_date)

    def _refresh_attractions(self, agent_input: ReviseAgentInput, intent: RevisionIntent):
        existing = []
        current_draft = agent_input.current_draft
        if current_draft:
            existing_titles = [
                block.title
                for day in current_draft.day_plans
                for block in day.time_blocks
                if block.item_type == "attraction" and block.title not in intent.removed_spots
            ]
            existing = [{"name": title} for title in existing_titles]

        preferences = list(agent_input.request.preferences or []) + list(agent_input.request.optional_spots or [])
        preferences.extend(intent.style_shift)
        preferences.extend(intent.added_spots)
        if intent.pace_change == "slower":
            preferences.append("轻松节奏")
        if intent.pace_change == "faster":
            preferences.append("高效串联")

        return attraction_tool.run(
            AttractionInput(
                city=agent_input.request.destination,
                days=agent_input.request.days,
                must_visit_spots=list(agent_input.request.must_visit_spots or []) + list(intent.locked_spots or []),
                avoid_spots=list(agent_input.request.avoid_spots or []) + list(intent.removed_spots or []),
                preferences=preferences,
                existing_candidates=existing,
                target_count_min=8,
                target_count_max=12,
            )
        )

    def _refresh_lodging(self, agent_input: ReviseAgentInput):
        current_draft = agent_input.current_draft or self._recover_draft(agent_input)
        attraction_titles = [
            block.title
            for day in current_draft.day_plans
            for block in day.time_blocks
            if block.item_type == "attraction"
        ]
        spots = attraction_titles[:6]
        preferences = list(agent_input.request.preferences) + ["优先标准酒店", "适合作为全程锚点"]
        return lodging_tool.run(
            LodgingInput(
                destination=agent_input.request.destination,
                budget=agent_input.request.budget,
                preferences=preferences,
                avoid_spots=list(agent_input.request.avoid_spots) + ["民宿", "旅馆招待所"],
                spots=spots,
            )
        )

    def _refresh_transport(self, agent_input: ReviseAgentInput, impact: RevisionImpactAnalysis):
        current_draft = agent_input.current_draft or self._recover_draft(agent_input)
        route_pairs: list[tuple[str, str]] = []
        target_days = set(impact.affected_days)
        for day in current_draft.day_plans:
            if target_days and day.day_index not in target_days:
                continue
            attraction_titles = [block.title for block in day.time_blocks if block.item_type == "attraction"]
            for idx in range(len(attraction_titles) - 1):
                route_pairs.append((attraction_titles[idx], attraction_titles[idx + 1]))
        routes = []
        for from_name, to_name in route_pairs[:3]:
            if not from_name or not to_name or from_name == to_name:
                continue
            routes.append(transport_tool.run(city=agent_input.request.destination, from_name=from_name, to_name=to_name))
        return routes

    def _resolve_dates(self, request: PlanningRequest) -> tuple[str, str]:
        if request.start_date and request.end_date:
            return request.start_date, request.end_date
        start = date.today() + timedelta(days=7)
        end = start + timedelta(days=max(request.days - 1, 0))
        return str(start), str(end)

    def _build_impact_reason(self, intent: RevisionIntent, affected_days: list[int], requires_tool_refresh: bool) -> str:
        scope_label = {
            "block_level": "局部块级修改",
            "day_level": "单日/多日主线修改",
            "global": "全局改稿",
        }[intent.change_scope]
        tool_label = "需要补充工具证据" if requires_tool_refresh else "暂不需要补充工具证据"
        return f"{scope_label}；影响天数={affected_days or '未显式指定'}；{tool_label}。"

    def _build_summary(self, intent: RevisionIntent, impact: RevisionImpactAnalysis) -> str:
        return f"已完成 revise 骨架分析：scope={impact.scope}，目标={intent.revision_goal}。"


revise_agent = ReviseAgent()
