from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.agents.prompt.reflection import REFLECTION_AGENT_PROMPT
from app.agents.schema.reflection import ReflectionIssue, ReflectionLLMResult, ReflectionResult
from app.agents.sparse.reflection import build_reflection_user_prompt
from app.domain.common.itinerary import ItineraryDayPlan
from app.domain.context.planning import PlanningContext
from app.infrastructure.llm_client import get_llm_client


@dataclass(frozen=True)
class ReflectionCheck:
    """一条反射不变式：check 产出问题清单，scope 决定其归属域（用于按域过滤）"""

    name: str
    scope: str
    check: Callable[[PlanningContext, set[int] | None], list[ReflectionIssue]]


class PlanningReflection:
    def __init__(self) -> None:
        self._checks: list[ReflectionCheck] = [
            ReflectionCheck("candidate_volume", "attraction", self._check_candidate_volume),
            ReflectionCheck("lodging_presence", "lodging", self._check_lodging_presence),
            ReflectionCheck("basic_time_blocks", "daily_plan", self._check_basic_time_blocks),
            ReflectionCheck("late_attraction", "daily_plan", self._check_late_attraction),
            ReflectionCheck("day_area_consistency", "daily_plan", self._check_day_area_consistency),
            ReflectionCheck("sparse_days", "daily_plan", self._check_sparse_days),
            ReflectionCheck("must_visit_coverage", "attraction", self._check_must_visit_coverage),
            ReflectionCheck("cross_day_duplicate", "planning", self._check_cross_day_duplicates),
            ReflectionCheck("weather_alignment", "weather_alignment", self._check_weather_alignment),
            ReflectionCheck("weather_availability", "planning", self._check_weather_availability),
        ]

    def review(self, state: PlanningContext, scopes: set[str] | None = None, days: set[int] | None = None) -> ReflectionResult:
        llm_client = get_llm_client()
        fallback = self._rule_based_review(state, scopes=scopes, days=days)
        if scopes is not None:
            # 定向复检（收敛期间）：只跑规则不变式，不走整篇 LLM 评审
            return fallback
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
        return self._enrich_issues_with_local_context(state, enriched, scopes=scopes, days=days)

    def _rule_based_review(self, state: PlanningContext, scopes: set[str] | None = None, days: set[int] | None = None) -> ReflectionResult:
        issues: list[ReflectionIssue] = []
        for check in self._select_checks(scopes):
            issues.extend(check.check(state, days))

        repair_scope = self._build_repair_scope(issues)
        status = "revise" if repair_scope else "accept"
        keep_constraints = self._summarize_keep_constraints(state)
        suggestions = [issue.fix_hint or issue.message for issue in issues]
        suggestions.extend(keep_constraints)
        return ReflectionResult(status=status, issues=issues, suggestions=suggestions, repair_scope=repair_scope)

    def _select_checks(self, scopes: set[str] | None) -> list[ReflectionCheck]:
        if scopes is None:
            return list(self._checks)
        return [check for check in self._checks if check.scope in scopes]

    def _iter_days(self, state: PlanningContext, days: set[int] | None = None) -> list[ItineraryDayPlan]:
        if not state.draft:
            return []
        if days is None:
            return state.draft.day_plans
        return [day for day in state.draft.day_plans if day.day_index in days]

    def _check_candidate_volume(self, state: PlanningContext, days: set[int] | None = None) -> list[ReflectionIssue]:
        request = state.request
        attraction_count = len(state.attraction_result.candidates) if state.attraction_result else 0
        if attraction_count < min(request.days * 2, 8):
            return [
                ReflectionIssue(
                    code="few_attractions",
                    message="景点候选偏少，建议补充最小完整集合。",
                    severity="warning",
                    scope="attraction",
                    fix_hint="action=expand_candidates; target=all_days; preferred_area=mixed; 补充代表性景点到目标下限，优先补足到 8 个左右。",
                )
            ]
        return []

    def _check_lodging_presence(self, state: PlanningContext, days: set[int] | None = None) -> list[ReflectionIssue]:
        lodging_count = len(state.lodging_result.candidates) if state.lodging_result else 0
        if lodging_count < 1:
            return [
                ReflectionIssue(
                    code="missing_lodging",
                    message="缺少住宿候选。",
                    severity="error",
                    scope="lodging",
                    fix_hint="action=keep_lodging_context; target=all_days; 保留住宿锚点说明并确保最终行程中体现住宿约束。",
                )
            ]
        return []

    def _check_weather_availability(self, state: PlanningContext, days: set[int] | None = None) -> list[ReflectionIssue]:
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

    def _check_basic_time_blocks(self, state: PlanningContext, days: set[int] | None = None) -> list[ReflectionIssue]:
        issues: list[ReflectionIssue] = []
        if not state.draft:
            return issues
        for day in self._iter_days(state, days):
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

    def _check_late_attraction(self, state: PlanningContext, days: set[int] | None = None) -> list[ReflectionIssue]:
        issues: list[ReflectionIssue] = []
        if not state.draft:
            return issues
        for day in self._iter_days(state, days):
            late_titles = [
                block.title
                for block in day.time_blocks
                if block.item_type == "attraction" and block.start_time >= "21:30"
            ]
            if late_titles:
                issues.append(
                    ReflectionIssue(
                        code="late_attraction",
                        message=f"Day{day.day_index} 存在安排在 21:30 后的景点：{', '.join(late_titles)}。",
                        severity="error",
                        scope="daily_plan",
                        fix_hint=f"action=reorder_day; day_index={day.day_index}; preferred_area={day.primary_area or state.request.destination}; suspect_titles={','.join(late_titles)}; 将 21:30 后的景点前移，避免景点过晚。",
                    )
                )
        return issues

    def _check_day_area_consistency(self, state: PlanningContext, days: set[int] | None = None) -> list[ReflectionIssue]:
        issues: list[ReflectionIssue] = []
        if not state.draft:
            return issues
        for day in self._iter_days(state, days):
            attraction_blocks = [block for block in day.time_blocks if block.item_type == "attraction" and block.area]
            unique_areas = list(dict.fromkeys(block.area for block in attraction_blocks))
            if not unique_areas:
                continue
            if day.primary_area and any(area != day.primary_area for area in unique_areas):
                # 只把不符合主区域的景点列为 suspect，匹配主区域的景点放入 must_keep 保留，避免误删同区正确景点
                mismatched = [block for block in attraction_blocks if block.area != day.primary_area]
                matched = [block for block in attraction_blocks if block.area == day.primary_area]
                issues.append(
                    ReflectionIssue(
                        code="area_inconsistency",
                        message=f"Day{day.day_index} 的主区域与景点区域不一致：{', '.join(unique_areas)}。",
                        severity="error",
                        scope="daily_plan",
                        fix_hint=f"action=consolidate_area; day_index={day.day_index}; preferred_area={day.primary_area}; suspect_titles={','.join([block.title for block in mismatched])}; must_keep={','.join([block.title for block in matched])}; 将当天尽量收束到同一区域。",
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

    def _check_sparse_days(self, state: PlanningContext, days: set[int] | None = None) -> list[ReflectionIssue]:
        issues: list[ReflectionIssue] = []
        if not state.draft:
            return issues
        for day in self._iter_days(state, days):
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

    def _check_must_visit_coverage(self, state: PlanningContext, days: set[int] | None = None) -> list[ReflectionIssue]:
        if not state.draft:
            return []
        all_titles = {block.title for day in state.draft.day_plans for block in day.time_blocks if block.item_type == "attraction"}
        missing = [spot for spot in state.request.must_visit_spots if not any(spot in title or title in spot for title in all_titles)]
        if not missing:
            return []
        # 每个缺失必去景点定向到最佳插入天（与景点同区域的天优先，否则景点最少的天），
        # 让修复只重建那一个天，避免全量重建扰动已收敛的其它天
        spot_areas = {candidate.name: candidate.area for candidate in (state.attraction_result.candidates if state.attraction_result else [])}
        return [
            ReflectionIssue(
                code="missing_must_visit",
                message=f"缺少必去景点：{spot}。",
                severity="error",
                scope="attraction",
                fix_hint=f"action=insert_required_spot; day_index={self._pick_best_fit_day(state, spot, spot_areas.get(spot))}; must_keep={spot}; 将必去景点插入最匹配的一天。",
            )
            for spot in missing
        ]

    def _pick_best_fit_day(self, state: PlanningContext, spot: str, spot_area: str | None) -> int:
        """为必去景点挑选最合适的插入天：优先与景点同区域的天，否则选景点最少的天"""
        days = state.draft.day_plans
        if not days:
            return 1
        if spot_area:
            for day in days:
                if day.primary_area == spot_area:
                    return day.day_index
        return min(
            days,
            key=lambda day: len([b for b in day.time_blocks if b.item_type == "attraction"]),
        ).day_index

    def _check_cross_day_duplicates(self, state: PlanningContext, days: set[int] | None = None) -> list[ReflectionIssue]:
        """跨天不变式：同一景点不得在多个天重复安排（保留首次出现，后续天替换替代景点）

        finalize 会静默去重兜底，但这里先以检查-修复闭环暴露问题并定向重建后续天
        """
        issues: list[ReflectionIssue] = []
        if not state.draft:
            return issues
        seen_titles: dict[str, int] = {}
        for day in state.draft.day_plans:
            for block in day.time_blocks:
                if block.item_type != "attraction":
                    continue
                if block.title in seen_titles:
                    issues.append(
                        ReflectionIssue(
                            code="cross_day_duplicate",
                            message=f"{block.title} 在 Day{seen_titles[block.title]} 与 Day{day.day_index} 重复安排。",
                            severity="warning",
                            scope="planning",
                            fix_hint=f"action=consolidate_duplicate; day_index={day.day_index}; preferred_area={day.primary_area or state.request.destination}; suspect_titles={block.title}; 保留首次出现的安排，后续天替换为同区域替代景点。",
                        )
                    )
                else:
                    seen_titles[block.title] = day.day_index
        return issues

    def _check_weather_alignment(self, state: PlanningContext, days: set[int] | None = None) -> list[ReflectionIssue]:
        issues: list[ReflectionIssue] = []
        if not state.draft or not state.weather_result:
            return issues
        weather_days = state.weather_result.daily
        for day in self._iter_days(state, days):
            weather_index = day.day_index - 1
            if weather_index < 0 or weather_index >= len(weather_days):
                continue
            weather = weather_days[weather_index].weather_day or ""
            high_temp = self._extract_high_temperature(weather_days[weather_index].temperature_range)
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

    def _enrich_issues_with_local_context(
        self,
        state: PlanningContext,
        result: ReflectionResult,
        scopes: set[str] | None = None,
        days: set[int] | None = None,
    ) -> ReflectionResult:
        fallback = self._rule_based_review(state, scopes=scopes, days=days)
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
