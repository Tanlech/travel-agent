from __future__ import annotations

import json
from typing import Any

from app.agents.schema.planning import PlanningRequest, TripPlan
from app.agents.schema.revise import RevisionImpactAnalysis, RevisionIntent
from app.infrastructure.llm.schemas import ItineraryDraftSchema


def build_revision_intent_prompt(
    *,
    request: PlanningRequest,
    current_plan: TripPlan,
    current_draft: ItineraryDraftSchema | None,
    raw_revision_message: str,
    user_context: Any,
    session_context: Any,
    trip_history: list[Any] | None = None,
) -> str:
    draft_summary = _summarize_draft(current_draft) if current_draft else None
    plan_summary = _summarize_plan(current_plan)

    parts = [
        "[request]",
        json.dumps(request.model_dump(), ensure_ascii=False, indent=2),
        "",
        "[current_plan_summary]",
        json.dumps(plan_summary, ensure_ascii=False, indent=2),
        "",
        "[current_draft_summary]",
        json.dumps(draft_summary, ensure_ascii=False, indent=2),
        "",
        "[user_context]",
        json.dumps(_model_dump_any(user_context), ensure_ascii=False, indent=2),
        "",
        "[session_context]",
        json.dumps(_model_dump_any(session_context), ensure_ascii=False, indent=2),
    ]
    if trip_history:
        parts.extend(
            [
                "",
                "[trip_history] 历史行程记忆（按目的地去重，供参考已去过哪/避坑）",
                json.dumps(trip_history, ensure_ascii=False, indent=2),
            ]
        )
    parts.extend(["", "[raw_revision_message]", raw_revision_message])
    return "\n".join(parts)


def build_block_level_revise_prompt(
    *,
    request: PlanningRequest,
    current_plan: TripPlan,
    current_draft: ItineraryDraftSchema,
    revision_intent: RevisionIntent,
    impact: RevisionImpactAnalysis,
    refreshed_context: dict[str, Any],
) -> str:
    affected_days = _slice_day_plans(current_draft, impact.affected_days)
    plan_summary = _summarize_plan(current_plan)

    parts = [
        "[request]",
        json.dumps(request.model_dump(), ensure_ascii=False, indent=2),
        "",
        "[current_plan_summary]",
        json.dumps(plan_summary, ensure_ascii=False, indent=2),
        "",
        "[affected_day_plans]",
        json.dumps(affected_days, ensure_ascii=False, indent=2),
        "",
        "[revision_intent]",
        json.dumps(revision_intent.model_dump(), ensure_ascii=False, indent=2),
        "",
        "[impact_analysis]",
        json.dumps(impact.model_dump(), ensure_ascii=False, indent=2),
        "",
        "[refreshed_context]",
        json.dumps(refreshed_context, ensure_ascii=False, indent=2),
        "",
        "[revision_task]",
        "只修改受影响的 day_plans / block，不要重写整份 itinerary，未受影响的天必须保持不变。",
    ]
    return "\n".join(parts)


def build_day_level_revise_prompt(
    *,
    request: PlanningRequest,
    current_plan: TripPlan,
    current_draft: ItineraryDraftSchema,
    revision_intent: RevisionIntent,
    impact: RevisionImpactAnalysis,
    refreshed_context: dict[str, Any],
) -> str:
    affected_days = _slice_day_plans(current_draft, impact.affected_days)
    reused_days = _slice_day_plans(current_draft, impact.reused_days)
    plan_summary = _summarize_plan(current_plan)

    parts = [
        "[request]",
        json.dumps(request.model_dump(), ensure_ascii=False, indent=2),
        "",
        "[current_plan_summary]",
        json.dumps(plan_summary, ensure_ascii=False, indent=2),
        "",
        "[affected_day_plans]",
        json.dumps(affected_days, ensure_ascii=False, indent=2),
        "",
        "[reused_day_summaries]",
        json.dumps(_summarize_day_plan_list(reused_days), ensure_ascii=False, indent=2),
        "",
        "[revision_intent]",
        json.dumps(revision_intent.model_dump(), ensure_ascii=False, indent=2),
        "",
        "[impact_analysis]",
        json.dumps(impact.model_dump(), ensure_ascii=False, indent=2),
        "",
        "[refreshed_context]",
        json.dumps(refreshed_context, ensure_ascii=False, indent=2),
        "",
        "[revision_task]",
        "只重做受影响的 day_plans，未受影响的天默认锁定，优先最小必要改动。",
    ]
    return "\n".join(parts)


def build_global_revise_prompt(
    *,
    request: PlanningRequest,
    current_plan: TripPlan,
    current_draft: ItineraryDraftSchema,
    revision_intent: RevisionIntent,
    impact: RevisionImpactAnalysis,
    refreshed_context: dict[str, Any],
) -> str:
    parts = [
        "[request]",
        json.dumps(request.model_dump(), ensure_ascii=False, indent=2),
        "",
        "[current_plan_summary]",
        json.dumps(_summarize_plan(current_plan), ensure_ascii=False, indent=2),
        "",
        "[current_draft_summary]",
        json.dumps(_summarize_draft(current_draft), ensure_ascii=False, indent=2),
        "",
        "[revision_intent]",
        json.dumps(revision_intent.model_dump(), ensure_ascii=False, indent=2),
        "",
        "[impact_analysis]",
        json.dumps(impact.model_dump(), ensure_ascii=False, indent=2),
        "",
        "[refreshed_context]",
        json.dumps(refreshed_context, ensure_ascii=False, indent=2),
        "",
        "[revision_task]",
        "这是 constrained global revise：尽量复用原 itinerary 的有效结构，只在满足修改目标所必需时才大范围重排。",
    ]
    return "\n".join(parts)


def _summarize_plan(plan: TripPlan) -> dict[str, Any]:
    return {
        "destination": plan.destination,
        "summary": plan.summary,
        "selected_day_areas": [day.get("primary_area") for day in plan.daily_plan],
        "day_titles": [
            {
                "day_index": day.get("day_index"),
                "titles": [block.get("title") for block in day.get("time_blocks", []) if block.get("title")],
            }
            for day in plan.daily_plan
        ],
    }


def _summarize_draft(draft: ItineraryDraftSchema) -> dict[str, Any]:
    return {
        "destination": draft.destination,
        "summary": draft.summary,
        "selected_day_areas": list(draft.selected_day_areas),
        "day_plans": _summarize_day_plan_list([day.model_dump() for day in draft.day_plans]),
    }


def _summarize_day_plan_list(day_plans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    for day in day_plans:
        blocks = day.get("time_blocks", []) or []
        summarized.append(
            {
                "day_index": day.get("day_index"),
                "primary_area": day.get("primary_area"),
                "block_count": len(blocks),
                "attraction_titles": [block.get("title") for block in blocks if block.get("item_type") == "attraction"],
                "meal_titles": [block.get("title") for block in blocks if block.get("item_type") == "meal"],
                "start_time": blocks[0].get("start_time") if blocks else None,
                "end_time": blocks[-1].get("end_time") if blocks else None,
            }
        )
    return summarized


def _slice_day_plans(draft: ItineraryDraftSchema, day_indices: list[int]) -> list[dict[str, Any]]:
    selected = []
    day_set = set(day_indices)
    for day in draft.day_plans:
        if day.day_index in day_set:
            selected.append(day.model_dump())
    return selected


def _model_dump_any(value: Any) -> Any:
    if value is None:
        return None
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    return value
