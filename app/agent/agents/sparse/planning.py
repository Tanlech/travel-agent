from __future__ import annotations

import json
from typing import Any

from app.agent.agents.schema.planning import ClusterPlanInput, FinalItineraryRenderInput, PlanLoopInput, SkeletonPlanInput


def build_plan_loop_prompt(payload: PlanLoopInput) -> str:
    parts = [
        "[request]",
        json.dumps(payload.request, ensure_ascii=False, indent=2),
        "",
        "[collected_tools]",
        json.dumps(payload.collected_tools, ensure_ascii=False, indent=2),
        "",
        "[observation_log]",
        json.dumps(payload.observation_log[-8:], ensure_ascii=False, indent=2),
    ]
    if payload.current_hypothesis:
        parts.extend([
            "",
            "[current_hypothesis]",
            payload.current_hypothesis,
        ])
    return "\n".join(parts)


def build_cluster_plan_prompt(payload: ClusterPlanInput) -> str:
    attraction_summary = _summarize_attraction_candidates(payload.attraction_candidates)
    lodging_summary = _summarize_lodging_candidates(payload.lodging_candidates, payload.selected_lodging)
    weather_summary = _summarize_weather(payload.weather)
    parts = [
        "[request]",
        json.dumps(payload.request, ensure_ascii=False, indent=2),
        "",
        "[attraction_candidates_summary]",
        json.dumps(attraction_summary, ensure_ascii=False, indent=2),
        "",
        "[weather_summary]",
        json.dumps(weather_summary, ensure_ascii=False, indent=2),
        "",
        "[lodging_summary]",
        json.dumps(lodging_summary, ensure_ascii=False, indent=2),
        "",
        "[observation_log]",
        json.dumps(payload.observation_log[-10:], ensure_ascii=False, indent=2),
    ]
    return "\n".join(parts)


def build_skeleton_prompt(payload: SkeletonPlanInput) -> str:
    attraction_summary = _summarize_attraction_candidates(payload.attraction_candidates)
    lodging_summary = _summarize_lodging_candidates(payload.lodging_candidates, payload.selected_lodging)
    weather_summary = _summarize_weather(payload.weather)
    transport_summary = _summarize_transport_evidence(payload.transport_evidence)
    day_completeness_rules = {
        "goal": "基于 cluster_plans 做完整可用旅行日骨架，而不是重新从散点组织。",
        "requirements": [
            "当前阶段所有天都视为完整旅行日，不要自行推断到达日、离开日、整理行李日或提前返程日",
            "默认同时规划上午主线、午餐、下午主线、傍晚过渡、晚餐、晚间正式活动/夜游/夜景/茶馆/商圈收尾，以及回酒店",
            "默认结束时间应接近 21:00–22:00，而不是 18:30–19:30",
            "如果下午主线较早结束，不要直接收掉当天；必须自然展开傍晚与晚间时段，避免长时间空窗",
            "不要为了晚上硬塞不合适景点，但也不要只补一个很短的夜间块来敷衍闭合",
            "如果当前住宿与多数天主簇明显冲突，应通过 needs_lodging_refresh 与 lodging_refresh_reason 显式指出",
            "如果存在关键远郊/跨区/晚间返程不确定链路，应通过 transport_check_request 显式指出，而不是默认查全量 transport",
        ],
    }

    parts = [
        "[request]",
        json.dumps(payload.request, ensure_ascii=False, indent=2),
        "",
        "[cluster_plans]",
        json.dumps(payload.cluster_plans, ensure_ascii=False, indent=2),
        "",
        "[attraction_candidates_summary]",
        json.dumps(attraction_summary, ensure_ascii=False, indent=2),
        "",
        "[weather_summary]",
        json.dumps(weather_summary, ensure_ascii=False, indent=2),
        "",
        "[lodging_summary]",
        json.dumps(lodging_summary, ensure_ascii=False, indent=2),
        "",
        "[selected_lodging_status]",
        json.dumps(payload.selected_lodging_status, ensure_ascii=False),
        "",
        "[transport_evidence_summary]",
        json.dumps(transport_summary, ensure_ascii=False, indent=2),
        "",
        "[planning_budgets]",
        json.dumps(payload.planning_budgets, ensure_ascii=False, indent=2),
        "",
        "[day_completeness_rules]",
        json.dumps(day_completeness_rules, ensure_ascii=False, indent=2),
        "",
        "[observation_log]",
        json.dumps(payload.observation_log[-10:], ensure_ascii=False, indent=2),
    ]
    return "\n".join(parts)


def build_itinerary_render_prompt(payload: FinalItineraryRenderInput) -> str:
    rendering_requirements = {
        "goal": "把 skeleton 渲染成结构化 itinerary，detail 要具体但简洁，避免长篇总结。",
        "schema_must_haves": [
            "每个 day_plan 都必须包含至少 1 个 transport、1 个 attraction、2 个 meal（通常为午餐+晚餐）、1 个 return 或 flex block",
            "time_blocks 必须按时间升序，且 start_time 小于 end_time",
            "day_plans 必须覆盖全部旅行天数",
            "如果不确定内容，优先输出更短但合法的字符串，不要省略结构",
        ],
        "attraction_detail": [
            "只写当前点本身，不要复述整天摘要",
            "优先写当前点怎么逛、为什么放这个时段、如何接下一站",
            "如有天气影响，只写对当前点最相关的调整",
            "detail 控制在 1-2 句",
        ],
        "transport": [
            "城市内交通只使用步行、地铁、打车三类表达",
            "尽量说明更推荐步行、地铁或打车中的哪一种",
            "补一句原因，例如顺路、少换乘、避雨、节省体力",
            "只有在 transport evidence 明确支持时，才写具体站名、线路、站数、分钟数或费用",
            "不要写空泛套话",
        ],
        "meal": [
            "尽量直接给出具体餐馆名、馆内简餐点或明确用餐点",
            "说明为什么安排在这里以及如何衔接前后景点",
            "detail 控制在 1-2 句",
        ],
        "day_flow": [
            "当前阶段所有天都按完整可用旅行日处理，不区分到达日或离开日",
            "默认结束时间应接近 21:00–22:00，而不是 18:30–19:30",
            "晚上是正式时段，每天默认显式包含晚餐和一个晚间正式活动/夜游/夜景/茶馆/商圈收尾，再返回酒店",
            "如果下午主线较早结束，不要直接收掉当天；必须自然展开傍晚过渡、晚餐与晚间段，避免长时间空窗或只补一个很短的夜间块",
        ],
        "notes": [
            "每一天通常保留 2-4 条高价值提醒",
            "优先保留预约、天气、风险、执行提醒、闭馆提醒、入场方式、限流/排队、携带物、避雨/防晒、换乘提醒",
            "每条 note 都要具体、简洁、可操作",
            "如果当天确有重要提醒，不要输出空 notes",
        ],
    }

    parts = [
        "[request]",
        json.dumps(payload.request, ensure_ascii=False, indent=2),
        "",
        "[planning_skeleton]",
        json.dumps(payload.skeleton, ensure_ascii=False, indent=2),
        "",
        "[weather]",
        json.dumps(payload.weather, ensure_ascii=False, indent=2),
        "",
        "[planning_anchor]",
        json.dumps(payload.planning_anchor, ensure_ascii=False, indent=2),
        "",
        "[selected_lodging_reference]",
        json.dumps(payload.selected_lodging, ensure_ascii=False, indent=2),
        "",
        "[rendering_requirements]",
        json.dumps(rendering_requirements, ensure_ascii=False, indent=2),
    ]

    if payload.transport_evidence:
        parts.extend([
            "",
            "[transport_evidence]",
            json.dumps(payload.transport_evidence, ensure_ascii=False, indent=2),
        ])

    return "\n".join(parts)


def _summarize_attraction_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summarized: list[dict[str, Any]] = []
    for item in candidates[:15]:
        name = item.get("name")
        area = item.get("area")
        duration = item.get("estimated_visit_duration_hours")
        reason = str(item.get("reason") or "")
        summarized.append(
            {
                "name": name,
                "area": area,
                "estimated_visit_duration_hours": duration,
                "reason_summary": reason[:180],
                "is_remote_heavy": _is_remote_heavy(area, name, duration),
                "is_rain_friendly": _is_rain_friendly(name, reason),
                "is_light_experience": _is_light_experience(duration, reason),
            }
        )
    return summarized


def _summarize_lodging_candidates(candidates: list[dict[str, Any]], selected_lodging: dict[str, Any] | None) -> dict[str, Any]:
    fields = ("poi_id", "name", "area", "price", "address", "tel")
    return {
        "provisional_top_candidate": (
            {field: selected_lodging.get(field) for field in fields if selected_lodging.get(field) is not None}
            if selected_lodging
            else None
        ),
        "candidates": [
            {field: item.get(field) for field in fields if item.get(field) is not None}
            for item in candidates[:5]
        ],
        "instruction": "候选已由 lodging_tool 按当前条件筛选排序；请结合完整行程自行选择，不要虚构候选外酒店。",
    }


def _summarize_weather(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "date": item.get("date"),
            "weather": item.get("weather"),
            "temperature_range": item.get("temperature_range"),
            "risk": _weather_risk(item.get("weather")),
        }
        for item in items[:7]
    ]


def _summarize_transport_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return items[:10]


def _is_remote_heavy(area: Any, name: Any, duration: Any) -> bool:
    area_text = str(area or "")
    name_text = str(name or "")
    hours = float(duration or 0)
    return area_text in {"延庆区", "怀柔区", "密云区", "平谷区"} or "长城" in name_text or hours >= 4.5


def _is_rain_friendly(name: Any, reason: Any) -> bool:
    text = f"{name or ''} {reason or ''}"
    return any(keyword in text for keyword in ["博物馆", "展", "室内", "馆"])


def _is_light_experience(duration: Any, reason: Any) -> bool:
    hours = float(duration or 0)
    reason_text = str(reason or "")
    return hours <= 1.8 or any(keyword in reason_text for keyword in ["慢行", "步行", "轻松", "漫步"])


def _weather_risk(weather_text: Any) -> str:
    text = str(weather_text or "")
    if any(keyword in text for keyword in ["雷阵雨", "中雨", "大雨", "暴雨"]):
        return "high"
    if any(keyword in text for keyword in ["小雨", "阵雨"]):
        return "medium"
    return "low"
