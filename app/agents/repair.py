from __future__ import annotations

from dataclasses import dataclass, field

from app.agents.prompt.repair import REPAIR_AGENT_PROMPT
from app.agents.schema.repair import RepairResult
from app.agents.sparse.repair import build_repair_user_prompt
from app.domain.context.planning import PlanningContext
from app.domain.common.itinerary import ItineraryDayPlan, ItineraryDraftSchema, ItineraryTimeBlockSchema
from app.infrastructure.llm_client import get_llm_client
from app.tools.attraction import attraction_tool
from app.tools.schema.attraction import AttractionCandidate, AttractionInput


@dataclass
class RepairTask:
    day_index: int | None = None
    problem_type: str = "generic"
    action: str = "rebuild_day"
    preferred_area: str | None = None
    must_keep_titles: list[str] = field(default_factory=list)
    remove_titles: list[str] = field(default_factory=list)
    weather_constraint: str | None = None


class PlanningRepair:
    def repair(self, state: PlanningContext) -> PlanningContext:
        repair_tasks = self._build_repair_plan(state)
        llm_client = get_llm_client()
        if llm_client.is_enabled():
            user_prompt = build_repair_user_prompt(
                request=state.request.model_dump(),
                draft=state.draft.model_dump(),
                reflection=state.reflection_result.model_dump(),
                attraction_candidates=[item.model_dump() for item in (state.attraction_result.candidates if state.attraction_result else [])],
                lodging_candidates=[item.model_dump() for item in (state.lodging_result.candidates if state.lodging_result else [])],
                weather=[item.model_dump() for item in (state.weather_result.daily if state.weather_result else [])],
            )
            result = llm_client.generate_repair_proposal(system_prompt=REPAIR_AGENT_PROMPT, user_prompt=user_prompt)
            if result is not None:
                state = self._apply_repair_result(state, RepairResult(**result.model_dump()), repair_tasks)
                state.trace.append({"step": "repair_llm", "modified_days": result.modified_days})
                return state

        state = self._rule_based_repair(state, repair_tasks)
        state.trace.append({"step": "repair", "repair_scope": list(getattr(state.reflection_result, "repair_scope", []) or []), "revision_count": state.revision_count})
        return state

    def _build_repair_plan(self, state: PlanningContext) -> list[RepairTask]:
        tasks: list[RepairTask] = []
        for issue in getattr(state.reflection_result, "issues", []) or []:
            task = RepairTask(problem_type=issue.code)
            fix_hint = issue.fix_hint or ""
            parsed = self._parse_fix_hint(fix_hint)
            task.day_index = self._safe_int(parsed.get("day_index"))
            task.action = parsed.get("action", self._default_action_for_issue(issue.code))
            task.preferred_area = parsed.get("preferred_area")
            task.must_keep_titles = self._split_csv(parsed.get("must_keep"))
            task.remove_titles = self._split_csv(parsed.get("suspect_titles"))
            task.weather_constraint = parsed.get("weather")
            tasks.append(task)
        return tasks

    def _parse_fix_hint(self, text: str) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for part in text.split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            parsed[key.strip()] = value.strip()
        return parsed

    def _safe_int(self, value: str | None) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            return None

    def _split_csv(self, value: str | None) -> list[str]:
        if not value:
            return []
        return [item.strip() for item in value.split(",") if item.strip()]

    def _default_action_for_issue(self, code: str) -> str:
        mapping = {
            "few_attractions": "expand_candidates",
            "missing_must_visit": "insert_required_spot",
            "area_inconsistency": "consolidate_area",
            "multi_area_jump": "rebuild_day",
            "sparse_day": "fill_day",
            "missing_meal_block": "add_meal",
            "missing_return_block": "add_return",
            "weather_misalignment": "weather_adjust",
            "heat_sparse_risk": "fill_day",
            "sub_spot_present": "replace_sub_spots",
        }
        return mapping.get(code, "rebuild_day")

    def _rule_based_repair(self, state: PlanningContext, repair_tasks: list[RepairTask]) -> PlanningContext:
        if any(task.action in {"expand_candidates", "replace_sub_spots", "insert_required_spot"} for task in repair_tasks):
            self._refresh_candidates(state)

        all_days = {day.day_index: day for day in state.draft.day_plans}
        target_days = self._collect_target_days(state, repair_tasks)
        for day_index in target_days:
            relevant_tasks = [task for task in repair_tasks if task.day_index in (None, day_index)]
            all_days[day_index] = self._rebuild_day_plan(state, day_index, relevant_tasks, all_days.get(day_index))

        state.draft.day_plans = [all_days[idx] for idx in sorted(all_days)]
        state.draft = self._finalize_draft(state, state.draft)
        state.revision_count += 1
        return state

    def _refresh_candidates(self, state: PlanningContext) -> None:
        request = state.request
        existing = [item for item in (state.attraction_result.candidates if state.attraction_result else []) if item.entity_level != "sub"]
        existing_names = [item.name for item in existing]
        attraction_result = attraction_tool.run(
            AttractionInput(
                city=request.destination,
                days=request.days,
                must_visit_spots=request.must_visit_spots,
                avoid_spots=request.avoid_spots,
                preferences=request.preferences + request.optional_spots + ["城市地标", "文旅街区", "室内博物馆"],
                existing_candidates=existing,
                target_count_min=8,
                target_count_max=12,
            )
        )
        if attraction_result.candidates and state.attraction_result:
            merged = list(existing)
            for candidate in attraction_result.candidates:
                if candidate.entity_level == "sub":
                    continue
                if candidate.name not in existing_names:
                    merged.append(candidate)
                    existing_names.append(candidate.name)
            state.attraction_result.candidates = merged[:12]

    def _collect_target_days(self, state: PlanningContext, repair_tasks: list[RepairTask]) -> list[int]:
        explicit = {task.day_index for task in repair_tasks if task.day_index is not None}
        if explicit:
            return sorted(explicit)
        return [day.day_index for day in state.draft.day_plans]

    def _apply_repair_result(self, state: PlanningContext, result: RepairResult, repair_tasks: list[RepairTask]) -> PlanningContext:
        candidates = list(state.attraction_result.candidates if state.attraction_result else [])
        candidate_by_index = {idx: candidate for idx, candidate in enumerate(candidates)}
        candidate_by_poi_id = {candidate.poi_id: candidate for candidate in candidates if candidate.poi_id}
        candidate_by_name = {candidate.name: candidate for candidate in candidates}

        day_map = {day.day_index: day for day in state.draft.day_plans}
        rebuilt_days: dict[int, ItineraryDayPlan] = {}
        for repaired_day in result.day_plans:
            base_day = day_map.get(repaired_day.day_index)
            resolved_candidates: list[AttractionCandidate] = []
            for spot_ref in repaired_day.spots:
                matched_candidate = None
                if spot_ref.candidate_index is not None:
                    matched_candidate = candidate_by_index.get(spot_ref.candidate_index)
                if matched_candidate is None and spot_ref.poi_id:
                    matched_candidate = candidate_by_poi_id.get(spot_ref.poi_id)
                if matched_candidate is None and spot_ref.poi_id:
                    matched_candidate = candidate_by_name.get(spot_ref.poi_id)
                if matched_candidate is not None:
                    resolved_candidates.append(matched_candidate)
                else:
                    fallback_title = spot_ref.poi_id or (f"候选点#{spot_ref.candidate_index}" if spot_ref.candidate_index is not None else "候选景点")
                    resolved_candidates.append(
                        AttractionCandidate(
                            poi_id=spot_ref.poi_id,
                            name=fallback_title,
                            area=repaired_day.primary_area,
                            estimated_visit_duration_hours=2.0,
                            reason=spot_ref.reason,
                        )
                    )
            relevant_tasks = [task for task in repair_tasks if task.day_index in (None, repaired_day.day_index)]
            rebuilt_days[repaired_day.day_index] = self._rebuild_day_plan(
                state,
                repaired_day.day_index,
                relevant_tasks,
                base_day,
                preferred_area=repaired_day.primary_area,
                preferred_candidates=resolved_candidates,
                override_notes=list(repaired_day.notes),
            )

        for day_index in self._collect_target_days(state, repair_tasks):
            if day_index not in rebuilt_days:
                base_day = day_map.get(day_index)
                relevant_tasks = [task for task in repair_tasks if task.day_index in (None, day_index)]
                rebuilt_days[day_index] = self._rebuild_day_plan(state, day_index, relevant_tasks, base_day)

        for idx, day in rebuilt_days.items():
            day_map[idx] = day

        state.draft.day_plans = [day_map[idx] for idx in sorted(day_map)]
        state.draft.summary = result.summary or self._rebuild_summary_from_days(state.draft)
        state.draft.selected_day_areas = [day.primary_area or state.request.destination for day in state.draft.day_plans]
        state.draft = self._finalize_draft(state, state.draft)
        state.draft.summary = self._rebuild_summary_from_days(state.draft)
        state.revision_count += 1
        return state

    def _rebuild_day_plan(
        self,
        state: PlanningContext,
        day_index: int,
        tasks: list[RepairTask],
        base_day: ItineraryDayPlan | None,
        *,
        preferred_area: str | None = None,
        preferred_candidates: list[AttractionCandidate] | None = None,
        override_notes: list[str] | None = None,
    ) -> ItineraryDayPlan:
        request = state.request
        candidates = list(state.attraction_result.candidates if state.attraction_result else [])
        weather_days = state.weather_result.daily if state.weather_result else []
        day_weather = weather_days[day_index - 1] if 0 <= day_index - 1 < len(weather_days) else None
        weather = day_weather.weather_day if day_weather else ""

        must_keep = {title for task in tasks for title in task.must_keep_titles}
        remove_titles = {title for task in tasks for title in task.remove_titles}
        chosen_area = preferred_area or next((task.preferred_area for task in tasks if task.preferred_area), None) or (base_day.primary_area if base_day else None) or request.destination

        selected: list[AttractionCandidate] = []
        pool = preferred_candidates if preferred_candidates is not None else candidates

        def can_use(candidate: AttractionCandidate) -> bool:
            if candidate.name in remove_titles:
                return False
            if any(avoid and (avoid in candidate.name or (candidate.reason and avoid in candidate.reason)) for avoid in request.avoid_spots):
                return False
            return True

        for title in must_keep:
            matched = next((candidate for candidate in candidates if candidate.name == title or title in candidate.name or candidate.name in title), None)
            if matched and can_use(matched) and matched.name not in {item.name for item in selected}:
                selected.append(matched)

        same_area = [candidate for candidate in pool if can_use(candidate) and (candidate.area == chosen_area or not candidate.area)]
        adjacent = [candidate for candidate in pool if can_use(candidate) and candidate.area != chosen_area]
        ranked_pool = same_area + adjacent

        for candidate in ranked_pool:
            if candidate.name in {item.name for item in selected}:
                continue
            if any(keyword in weather for keyword in ["雷阵雨", "暴雨", "阵雨"]) and any(token in (candidate.name + (candidate.reason or "")) for token in ["长城", "公园", "广场", "胡同"]) and len(selected) >= 1:
                continue
            selected.append(candidate)
            if len(selected) >= 3:
                break

        if not selected and pool:
            selected.append(pool[0])

        # 保留原 day 的 transport/meal/收尾等非景点块，只替换景点块，保证块结构完整
        blocks: list[ItineraryTimeBlockSchema] = [
            block
            for block in (base_day.time_blocks if base_day else [])
            if block.item_type != "attraction"
        ]
        # 若 base 缺失 transport 块，补一个前往当地区域的交通块，避免 block mix 校验失败
        if not any(block.item_type == "transport" for block in blocks):
            blocks.insert(
                0,
                ItineraryTimeBlockSchema(
                    start_time="09:00",
                    end_time="09:30",
                    item_type="transport",
                    title=f"前往{chosen_area}",
                    detail=f"前往{chosen_area}开始今日行程。",
                    area=chosen_area,
                ),
            )
        current_hour = 9
        for candidate in selected:
            duration = candidate.estimated_visit_duration_hours or 2.0
            end_hour = min(current_hour + max(int(round(duration)), 1), 20)
            blocks.append(
                ItineraryTimeBlockSchema(
                    start_time=f"{current_hour:02d}:00",
                    end_time=f"{end_hour:02d}:00",
                    item_type="attraction",
                    title=candidate.name,
                    detail=candidate.reason,
                    area=candidate.area or chosen_area,
                )
            )
            current_hour = min(end_hour + 1, 21)
        # 补齐 meal 与收尾块（若 base 缺失），保证校验通过
        if not any(block.item_type == "meal" for block in blocks):
            blocks.append(
                ItineraryTimeBlockSchema(
                    start_time=f"{current_hour:02d}:00",
                    end_time=f"{min(current_hour + 1, 21):02d}:00",
                    item_type="meal",
                    title="当地午餐",
                    detail="就近用餐，衔接下午行程。",
                    area=chosen_area,
                )
            )
        if not any(block.item_type in {"return", "flex"} for block in blocks):
            blocks.append(
                ItineraryTimeBlockSchema(
                    start_time="20:00",
                    end_time="21:00",
                    item_type="return",
                    title="返回住宿",
                    detail="结束当日行程，返回住宿休息。",
                    area=chosen_area,
                )
            )

        notes = list(override_notes or [])
        if not notes and base_day:
            notes = list(base_day.notes)
        if any(task.action == "weather_adjust" for task in tasks):
            notes.append("已按天气风险调整为更稳妥的动线，优先保留室内或可快速切换安排。")
        if any(task.action == "fill_day" for task in tasks):
            notes.append("已补强当日节奏，加入轻量文化节点或弹性休整段。")

        return ItineraryDayPlan(
            day_index=day_index,
            primary_area=chosen_area,
            time_blocks=blocks,
            notes=notes,
        )

    def _finalize_draft(self, state: PlanningContext, draft: ItineraryDraftSchema) -> ItineraryDraftSchema:
        weather_days = state.weather_result.daily if state.weather_result else []
        hotel = state.selected_lodging

        for day_idx, day in enumerate(draft.day_plans):
            if day_idx < len(weather_days):
                weather_day = weather_days[day_idx]
                weather_note = f"天气：{weather_day.weather_day} {weather_day.temperature_range or ''}".strip()
                day.notes = [note for note in day.notes if not note.startswith("天气：")]
                day.notes.insert(0, weather_note)
                if any(keyword in (weather_day.weather_day or "") for keyword in ["雷阵雨", "暴雨", "阵雨"]):
                    extra_note = "当日存在降雨风险，优先选择室内或半室内项目，并预留机动调整时间。"
                    if extra_note not in day.notes:
                        day.notes.append(extra_note)
            if hotel and not any("住宿" in note or hotel.name in note for note in day.notes):
                day.notes.append(f"优先住宿：{hotel.name}")
            self._normalize_day(day=day, hotel=hotel)

        self._deduplicate_across_days(draft)
        draft.selected_day_areas = [day.primary_area or state.request.destination for day in draft.day_plans]
        draft.summary = self._rebuild_summary_from_days(draft)
        return draft

    def _normalize_day(self, *, day: ItineraryDayPlan, hotel) -> None:
        attraction_blocks = [block for block in day.time_blocks if block.item_type == "attraction"]
        if not attraction_blocks:
            return

        normalized: list[ItineraryTimeBlockSchema] = []
        if hotel:
            normalized.append(
                ItineraryTimeBlockSchema(
                    start_time="08:30",
                    end_time="09:00",
                    item_type="transport",
                    title=f"从{hotel.name}前往{attraction_blocks[0].title}",
                    detail="建议优先地铁或打车，控制出发时间并避开拥堵。",
                    area=attraction_blocks[0].area or day.primary_area,
                    estimated_cost=25.0,
                )
            )

        current_hour = 9
        for idx, block in enumerate(attraction_blocks):
            start_hour = current_hour
            end_hour = min(start_hour + self._duration_hours(block), 20)
            normalized.append(
                ItineraryTimeBlockSchema(
                    start_time=f"{start_hour:02d}:00",
                    end_time=f"{end_hour:02d}:00",
                    item_type="attraction",
                    title=block.title,
                    detail=block.detail,
                    area=block.area or day.primary_area,
                    estimated_cost=block.estimated_cost,
                )
            )
            current_hour = min(end_hour + 1, 21)

            if idx == 0 and current_hour <= 13:
                normalized.append(
                    ItineraryTimeBlockSchema(
                        start_time=f"{current_hour:02d}:00",
                        end_time=f"{min(current_hour + 1, 21):02d}:00",
                        item_type="meal",
                        title="午餐与短暂休整",
                        detail=f"建议在{block.area or day.primary_area}选择本地口碑餐馆，避免连续高强度步行。",
                        area=block.area or day.primary_area,
                        estimated_cost=100.0,
                    )
                )
                current_hour = min(current_hour + 2, 21)

        if len(attraction_blocks) < 2 and current_hour <= 17:
            normalized.append(
                ItineraryTimeBlockSchema(
                    start_time=f"{current_hour:02d}:00",
                    end_time=f"{min(current_hour + 2, 20):02d}:00",
                    item_type="flex",
                    title="弹性活动/休整",
                    detail="可补充同片区轻量步行、展览馆或咖啡休整，保持轻松节奏。",
                    area=day.primary_area,
                )
            )
            current_hour = min(current_hour + 3, 21)

        if current_hour <= 19:
            normalized.append(
                ItineraryTimeBlockSchema(
                    start_time=f"{current_hour:02d}:00",
                    end_time=f"{min(current_hour + 1, 21):02d}:00",
                    item_type="meal",
                    title="晚餐",
                    detail=f"建议在{day.primary_area or ''}安排晚餐，优先选择可步行到交通点的餐厅。",
                    area=day.primary_area,
                    estimated_cost=120.0,
                )
            )
            current_hour = min(current_hour + 2, 21)

        if hotel:
            normalized.append(
                ItineraryTimeBlockSchema(
                    start_time=f"{max(min(current_hour, 21) - 1, 20):02d}:00",
                    end_time="21:00",
                    item_type="return",
                    title=f"返回{hotel.name}",
                    detail="建议当天最后一段优先打车或地铁返程，避免夜间多次换乘。",
                    area=hotel.area,
                    estimated_cost=30.0,
                )
            )

        day.time_blocks = sorted(normalized, key=lambda block: block.start_time)
        self._reconcile_notes(day)

    def _duration_hours(self, block: ItineraryTimeBlockSchema) -> int:
        try:
            start_hour = int(block.start_time.split(":")[0])
            end_hour = int(block.end_time.split(":")[0])
            return max(end_hour - start_hour, 1)
        except Exception:
            return 2

    def _reconcile_notes(self, day: ItineraryDayPlan) -> None:
        actual_areas = {block.area for block in day.time_blocks if block.area and block.item_type == "attraction"}
        cleaned: list[str] = []
        seen: set[str] = set()
        for note in day.notes:
            if note in seen:
                continue
            if "均位于" in note and len(actual_areas) > 1:
                continue
            seen.add(note)
            cleaned.append(note)
        day.notes = cleaned

    def _deduplicate_across_days(self, draft: ItineraryDraftSchema) -> None:
        seen_titles: set[str] = set()
        for day in draft.day_plans:
            unique_blocks: list[ItineraryTimeBlockSchema] = []
            for block in day.time_blocks:
                if block.item_type == "attraction":
                    if block.title in seen_titles:
                        continue
                    seen_titles.add(block.title)
                unique_blocks.append(block)
            day.time_blocks = unique_blocks

    def _rebuild_summary_from_days(self, draft: ItineraryDraftSchema) -> str:
        if not draft.day_plans:
            return draft.summary
        fragments: list[str] = []
        for day in draft.day_plans:
            titles = [block.title for block in day.time_blocks if block.item_type == "attraction"][:2]
            area = day.primary_area or draft.destination
            if titles:
                fragments.append(f"Day{day.day_index}聚焦{area}（{' + '.join(titles)}）")
            else:
                fragments.append(f"Day{day.day_index}聚焦{area}")
        return f"{len(draft.day_plans)}日{draft.destination}行程：" + "；".join(fragments) + "。"


planning_repair = PlanningRepair()
