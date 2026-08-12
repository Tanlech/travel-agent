from __future__ import annotations

from app.agents.prompt.reflection import REFLECTION_AGENT_PROMPT
from app.agents.schema.reflection import ReflectionIssue, ReflectionLLMResult, ReflectionResult
from app.agents.sparse.reflection import build_reflection_user_prompt
from app.domain.context.planning import PlanningContext
from app.infrastructure.llm.client import get_llm_client


class PlanningReflection:
    def review(self, state: PlanningContext) -> ReflectionResult:
        llm_client = get_llm_client()
        fallback = self._rule_based_review(state)
        if not llm_client.is_enabled():
            return fallback

        user_prompt = build_reflection_user_prompt(
            request=state.request.model_dump(),
            draft=state.draft.model_dump(),
            attraction_candidates=[item.model_dump() for item in (state.attraction_result.candidates if state.attraction_result else [])],
            lodging_candidates=[item.model_dump() for item in (state.lodging_result.candidates if state.lodging_result else [])],
            weather=[item.model_dump() for item in (state.weather_result.daily if state.weather_result else [])],
        )
        result = llm_client._generate_structured(
            schema=ReflectionLLMResult,
            system_prompt=REFLECTION_AGENT_PROMPT,
            user_prompt=user_prompt,
            retry_hints=[
                "Return JSON only.",
                "Do not add explanations before or after JSON.",
                "Use the exact schema fields.",
            ],
        )
        if result is None:
            return fallback
        enriched = ReflectionResult(
            status=result.status,
            issues=result.issues,
            suggestions=result.suggestions,
            repair_scope=result.repair_scope,
        )
        return self._enrich_issues_with_local_context(state, enriched)

    def _rule_based_review(self, state: PlanningContext) -> ReflectionResult:
        issues: list[ReflectionIssue] = []
        issues.extend(self._check_candidate_volume(state))
        issues.extend(self._check_basic_time_blocks(state))
        issues.extend(self._check_sub_spots(state))
        issues.extend(self._check_day_area_consistency(state))
        issues.extend(self._check_sparse_days(state))
        issues.extend(self._check_must_visit_coverage(state))
        issues.extend(self._check_weather_alignment(state))
        issues.extend(self._check_weather_availability(state))

        repair_scope = self._build_repair_scope(issues)
        status = "revise" if repair_scope else "accept"
        keep_constraints = self._summarize_keep_constraints(state)
        suggestions = [issue.fix_hint or issue.message for issue in issues]
        suggestions.extend(keep_constraints)
        return ReflectionResult(status=status, issues=issues, suggestions=suggestions, repair_scope=repair_scope)

    def _check_candidate_volume(self, state: PlanningContext) -> list[ReflectionIssue]:
        issues: list[ReflectionIssue] = []
        request = state.request
        attraction_count = len(state.attraction_result.candidates) if state.attraction_result else 0
        lodging_count = len(state.lodging_result.candidates) if state.lodging_result else 0
        if attraction_count < min(request.days * 2, 8):
            issues.append(
                ReflectionIssue(
                    code="few_attractions",
                    message="景点候选偏少，建议补充最小完整集合。",
                    severity="warning",
                    scope="attraction",
                    fix_hint="action=expand_candidates; target=all_days; preferred_area=mixed; 补充代表性景点到目标下限，优先补足到 8 个左右。",
                )
            )
        if lodging_count < 1:
            issues.append(
                ReflectionIssue(
                    code="missing_lodging",
                    message="缺少住宿候选。",
                    severity="error",
                    scope="lodging",
                    fix_hint="action=keep_lodging_context; target=all_days; 保留住宿锚点说明并确保最终行程中体现住宿约束。",
                )
            )
        return issues

    def _check_weather_availability(self, state: PlanningContext) -> list[ReflectionIssue]:
        if state.weather_result and state.weather_result.error:
            return [
                ReflectionIssue(
                    code="weather_unavailable",
                    message="天气信息不可用，需谨慎规划。",
                    severity="warning",
                    scope="planning",
                    fix_hint="action=add_weather_caution; target=all_days; 保留天气风险提示，并避免过度依赖天气分流。",
                )
            ]
        return []

    def _check_basic_time_blocks(self, state: PlanningContext) -> list[ReflectionIssue]:
        issues: list[ReflectionIssue] = []
        if not state.draft:
            return issues
        for day in state.draft.day_plans:
            previous_start = None
            for block in day.time_blocks:
                if block.end_time <= block.start_time:
                    issues.append(
                        ReflectionIssue(
                            code="invalid_time_block",
                            message=f"Day{day.day_index} 存在结束时间早于或等于开始时间的时间块。",
                            severity="error",
                            scope="daily_plan",
                            fix_hint=f"action=reorder_day; day_index={day.day_index}; preferred_area={day.primary_area or state.request.destination}; 重新编排行程时间块，保证时间递增。",
                        )
                    )
                    break
                if previous_start and block.start_time < previous_start:
                    issues.append(
                        ReflectionIssue(
                            code="unordered_time_block",
                            message=f"Day{day.day_index} 时间块顺序异常。",
                            severity="warning",
                            scope="daily_plan",
                            fix_hint=f"action=reorder_day; day_index={day.day_index}; preferred_area={day.primary_area or state.request.destination}; 按时间顺序重排当天时间块。",
                        )
                    )
                    break
                previous_start = block.start_time
        return issues

    def _check_sub_spots(self, state: PlanningContext) -> list[ReflectionIssue]:
        if state.attraction_result and any(item.entity_level == "sub" for item in state.attraction_result.candidates[: max(len(state.attraction_result.candidates), 1)]):
            return [
                ReflectionIssue(
                    code="sub_spot_present",
                    message="结果中仍含部分从属景点，建议替换为主景点或独立景点。",
                    severity="warning",
                    scope="attraction",
                    fix_hint="action=replace_sub_spots; target=all_days; 优先替换从属景点，保留主景区或独立景点。",
                )
            ]
        return []

    def _check_day_area_consistency(self, state: PlanningContext) -> list[ReflectionIssue]:
        issues: list[ReflectionIssue] = []
        if not state.draft:
            return issues
        for day in state.draft.day_plans:
            attraction_areas = [block.area for block in day.time_blocks if block.item_type == "attraction" and block.area]
            unique_areas = list(dict.fromkeys(attraction_areas))
            if not unique_areas:
                continue
            if day.primary_area and any(area != day.primary_area for area in unique_areas):
                issues.append(
                    ReflectionIssue(
                        code="area_inconsistency",
                        message=f"Day{day.day_index} 的主区域与景点区域不一致：{', '.join(unique_areas)}。",
                        severity="error",
                        scope="daily_plan",
                        fix_hint=f"action=consolidate_area; day_index={day.day_index}; preferred_area={day.primary_area}; suspect_titles={','.join([block.title for block in day.time_blocks if block.item_type == 'attraction'])}; 将当天尽量收束到同一区域。",
                    )
                )
            elif len(unique_areas) > 2:
                issues.append(
                    ReflectionIssue(
                        code="multi_area_jump",
                        message=f"Day{day.day_index} 涉及区域过多：{', '.join(unique_areas)}。",
                        severity="warning",
                        scope="daily_plan",
                        fix_hint=f"action=rebuild_day; day_index={day.day_index}; preferred_area={unique_areas[0]}; 减少跨区跳跃，控制在同一区域或相邻区域。",
                    )
                )
        return issues

    def _check_sparse_days(self, state: PlanningContext) -> list[ReflectionIssue]:
        issues: list[ReflectionIssue] = []
        if not state.draft:
            return issues
        for day in state.draft.day_plans:
            attraction_blocks = [block for block in day.time_blocks if block.item_type == "attraction"]
            if len(attraction_blocks) < 2:
                issues.append(
                    ReflectionIssue(
                        code="sparse_day",
                        message=f"Day{day.day_index} 景点安排偏少，日程过薄。",
                        severity="warning",
                        scope="daily_plan",
                        fix_hint=f"action=fill_day; day_index={day.day_index}; preferred_area={day.primary_area or state.request.destination}; must_keep={','.join([block.title for block in attraction_blocks])}; 补充同片区轻量文化点或休整节点。",
                    )
                )
            block_types = {block.item_type for block in day.time_blocks}
            if attraction_blocks and "meal" not in block_types:
                issues.append(
                    ReflectionIssue(
                        code="missing_meal_block",
                        message=f"Day{day.day_index} 缺少餐饮收口块。",
                        severity="warning",
                        scope="daily_plan",
                        fix_hint=f"action=add_meal; day_index={day.day_index}; preferred_area={day.primary_area or state.request.destination}; 增加午餐或晚餐时间块。",
                    )
                )
            if attraction_blocks and "return" not in block_types:
                issues.append(
                    ReflectionIssue(
                        code="missing_return_block",
                        message=f"Day{day.day_index} 缺少返程收口块。",
                        severity="warning",
                        scope="daily_plan",
                        fix_hint=f"action=add_return; day_index={day.day_index}; preferred_area={day.primary_area or state.request.destination}; 增加返回住宿点的收口块。",
                    )
                )
        return issues

    def _check_must_visit_coverage(self, state: PlanningContext) -> list[ReflectionIssue]:
        if not state.draft:
            return []
        all_titles = {block.title for day in state.draft.day_plans for block in day.time_blocks if block.item_type == "attraction"}
        missing = [spot for spot in state.request.must_visit_spots if not any(spot in title or title in spot for title in all_titles)]
        if not missing:
            return []
        return [
            ReflectionIssue(
                code="missing_must_visit",
                message=f"缺少必去景点：{', '.join(missing)}。",
                severity="error",
                scope="attraction",
                fix_hint=f"action=insert_required_spot; target=best_fit_day; must_keep={','.join(missing)}; 将必去景点插入最匹配的一天。",
            )
        ]

    def _check_weather_alignment(self, state: PlanningContext) -> list[ReflectionIssue]:
        issues: list[ReflectionIssue] = []
        if not state.draft or not state.weather_result:
            return issues
        weather_days = state.weather_result.daily
        for index, day in enumerate(state.draft.day_plans):
            if index >= len(weather_days):
                break
            weather = weather_days[index].weather or ""
            high_temp = self._extract_high_temperature(weather_days[index].temperature_range)
            attraction_blocks = [block for block in day.time_blocks if block.item_type == "attraction"]
            if not attraction_blocks:
                continue
            outdoor_risk = any(any(keyword in (block.title + (block.detail or "")) for keyword in ["公园", "广场", "长城", "遗址", "胡同"]) for block in attraction_blocks)
            if any(keyword in weather for keyword in ["雷阵雨", "暴雨", "阵雨"]) and outdoor_risk:
                issues.append(
                    ReflectionIssue(
                        code="weather_misalignment",
                        message=f"Day{day.day_index} 在 {weather} 天气下存在较多户外安排。",
                        severity="warning",
                        scope="weather_alignment",
                        fix_hint=f"action=weather_adjust; day_index={day.day_index}; preferred_area={day.primary_area or state.request.destination}; 优先室内或半室内项目，压缩长时间露天活动。",
                    )
                )
            if high_temp is not None and high_temp >= 30 and len(attraction_blocks) == 1 and outdoor_risk:
                issues.append(
                    ReflectionIssue(
                        code="heat_sparse_risk",
                        message=f"Day{day.day_index} 高温且露天安排单一，缺少避暑缓冲。",
                        severity="warning",
                        scope="weather_alignment",
                        fix_hint=f"action=fill_day; day_index={day.day_index}; preferred_area={day.primary_area or state.request.destination}; 增加室内或水系缓冲安排。",
                    )
                )
        return issues

    def _extract_high_temperature(self, temperature_range: str | None) -> int | None:
        if not temperature_range:
            return None
        numbers = []
        current = ""
        for char in temperature_range:
            if char.isdigit():
                current += char
            elif current:
                numbers.append(int(current))
                current = ""
        if current:
            numbers.append(int(current))
        return max(numbers) if numbers else None

    def _build_repair_scope(self, issues: list[ReflectionIssue]) -> list[str]:
        scope: list[str] = []
        for issue in issues:
            mapped = issue.scope or "daily_plan"
            if mapped not in scope:
                scope.append(mapped)
        return scope

    def _summarize_keep_constraints(self, state: PlanningContext) -> list[str]:
        constraints: list[str] = []
        if not state.draft:
            return constraints
        must_keep = [spot for spot in state.request.must_visit_spots if spot]
        if must_keep:
            constraints.append("keep_constraints: must_keep=" + ",".join(must_keep))
        stable_days: list[str] = []
        for day in state.draft.day_plans:
            attraction_blocks = [block for block in day.time_blocks if block.item_type == "attraction"]
            attraction_areas = {block.area for block in attraction_blocks if block.area}
            if len(attraction_blocks) >= 2 and (not day.primary_area or len(attraction_areas) <= 1):
                stable_days.append(f"day{day.day_index}")
        if stable_days:
            constraints.append("keep_constraints: stable_days=" + ",".join(stable_days))
        return constraints

    def _enrich_issues_with_local_context(self, state: PlanningContext, result: ReflectionResult) -> ReflectionResult:
        fallback = self._rule_based_review(state)
        merged = list(result.issues)
        existing_codes = {issue.code for issue in merged}
        for issue in fallback.issues:
            if issue.code not in existing_codes:
                merged.append(issue)
        suggestions = list(result.suggestions)
        for suggestion in fallback.suggestions:
            if suggestion not in suggestions:
                suggestions.append(suggestion)
        repair_scope = list(result.repair_scope)
        for scope in fallback.repair_scope:
            if scope not in repair_scope:
                repair_scope.append(scope)
        status = "revise" if repair_scope else result.status
        return ReflectionResult(status=status, issues=merged, suggestions=suggestions, repair_scope=repair_scope)


planning_reflection = PlanningReflection()
