from __future__ import annotations

import os
import random
import re
import time as _time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

from app.agent.agents.prompt.planning import (
    PLAN_CLUSTER_PROMPT,
    PLAN_RENDER_PROMPT,
    PLAN_SKELETON_PROMPT,
    PLAN_TOOL_COLLECTION_PROMPT,
)
from app.agent.agents.schema.planning import (
    CandidateCluster,
    ClusterPlanInput,
    ClusterPlanning,
    DialogueDecision,
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
from app.agent.agents.reflection import planning_reflection
from app.agent.agents.repair import planning_repair
from app.agent.agents.sparse.planning import build_cluster_plan_prompt, build_itinerary_render_prompt, build_skeleton_prompt
from app.agent.knowledge import ATTRACTION_COLLECTION, knowledge_service
from app.agent.knowledge.ingest.common import QA_COLLECTION
from app.agent.domain.context.builder import context_builder
from app.agent.domain.context.planning import PlanningContext
from app.agent.domain.context.response import ResponseContext
from app.agent.domain.common.planning import compute_missing_fields, extract_plan_attractions
from app.agent.domain.common.itinerary import ItineraryDayPlan, ItineraryDraftSchema, ItineraryTimeBlockSchema
from app.agent.domain.common.transport import pick_transport_mode
from app.infrastructure.amap_client import amap_client
from app.infrastructure.llm_client import get_llm_client
from app.agent.domain.memory.manager import memory_manager
from app.observability.monitoring import app_logger
from app.agent.tools.attraction import attraction_tool
from app.agent.tools.lodging import lodging_tool
from app.agent.tools.meal import meal_tool
from app.agent.tools.schema.attraction import AttractionInput
from app.agent.tools.schema.lodging import LodgingInput
from app.agent.tools.schema.meal import MealInput
from app.agent.tools.schema.transport import TransportInput
from app.agent.tools.schema.weather import WeatherInput
from app.agent.tools.transport import transport_tool
from app.agent.tools.weather import weather_tool


@dataclass
class ConvergenceBudget:
    """统一收敛预算：跨阶段共享，LLM 修复与规则修复分开计量

    llm_rounds 供全局一致性 pass 的 LLM 修复（API 成本）；rule_rounds 供分日
    worker 与全局 pass 的规则式修复（CPU 成本）。各阶段从同一预算读取，
    保证总修复成本受控，避免阶段间叠加超支。
    """

    llm_rounds: int = 2
    rule_rounds: int = 3


class PlanningDegradedError(RuntimeError):
    """规划链路降级错误：与其抛内部 RuntimeError 崩掉整条链路，不如让调用方
    给用户一条可读、可行动的引导重试消息。user_message 即对外提示。"""

    def __init__(self, user_message: str, *, detail: str = ""):
        super().__init__(detail or user_message)
        self.user_message = user_message
        self.detail = detail


# 从用户偏好中抽取餐饮相关关键词，供 meal_tool 命中餐饮类别/名称使用
_MEAL_PREFERENCE_KW = (
    "火锅", "本地菜", "家常菜", "小吃", "海鲜", "烧烤", "麻辣", "清淡", "甜品",
    "面食", "川菜", "粤菜", "日料", "素食", "小龙虾", "烤鸭", "奶茶", "咖啡",
)


def _extract_meal_preferences(request: PlanningRequest) -> list[str]:
    """从规划请求里挑出属于餐饮语义的偏好词；没有则回退到『本地菜、小吃』"""
    sources = list(request.preferences) + list(getattr(request, "optional_spots", []))
    matched = [kw for kw in _MEAL_PREFERENCE_KW if any(kw in item for item in sources)]
    return matched[:6] or ["本地菜", "小吃"]


# 工具收集阶段的流式阶段标签（SSE 展示"正在干什么"）
_TOOL_STAGE_LABELS = {
    "weather_tool": "正在查询天气…",
    "attraction_tool": "正在检索景点…",
    "lodging_tool": "正在挑选住宿…",
    "meal_tool": "正在挑选美食…",
    "transport_tool": "正在核对交通…",
    "attraction": "正在检索景点…",
    "weather": "正在查询天气…",
    "lodging": "正在挑选住宿…",
    "meal": "正在挑选美食…",
}


class PlanningAgent:
    # 景点/住宿名 → (lng, lat) 地理编码进程内缓存，避免每次规划重复请求高德
    _geocode_cache: dict[tuple[str, str], tuple[float | None, float | None]] = {}

    def __init__(self) -> None:
        self._cluster_plan_prompt = PLAN_CLUSTER_PROMPT
        self._skeleton_prompt = PLAN_SKELETON_PROMPT
        self._render_prompt = PLAN_RENDER_PROMPT

    # 流式阶段事件发射（SSE 用）：回调异常不影响主流程
    @staticmethod
    def _emit_progress(progress: Callable[[str, dict], None] | None, label: str) -> None:
        if progress:
            try:
                progress("stage", {"label": label})
            except Exception:  # noqa: BLE001
                pass

    def run_pipeline(
        self,
        plan_input: PlanInput,
        progress: Callable[[str, dict], None] | None = None,
    ) -> dict:
        _stage_start = _time.perf_counter()
        # 分步计时：包装 progress，使每个 stage 自带 elapsed_ms，并逐条打入后端日志，
        # 便于观察规划各阶段耗时（计时器为本次请求闭包，天然并发安全）。
        raw_progress = progress
        _last_t = _time.perf_counter()

        def _timed_progress(kind: str, data: dict) -> None:
            nonlocal _last_t
            now = _time.perf_counter()
            el = int((now - _last_t) * 1000)
            _last_t = now
            outer = dict(data)
            if kind == "stage" and outer.get("label"):
                app_logger.info("planning_stage", stage=outer["label"], duration_ms=el)
            outer["elapsed_ms"] = el
            if raw_progress:
                try:
                    raw_progress(kind, outer)
                except Exception:  # noqa: BLE001
                    pass

        progress = _timed_progress
        request = self._normalize_request(plan_input.request)
        state = context_builder.build_planning_context(request, user_id=plan_input.user_id, session_id=plan_input.session_id)
        app_logger.info(
            "planning_pipeline_start",
            destination=request.destination,
            days=request.days,
            travelers=request.travelers[0].split()[0] if request.travelers else None,
            session_id=plan_input.session_id,
        )
        # 只读字段校验兜底（对话阶段判定已在 orchestrator 侧由 SessionStateService 完成）
        dialogue_decision, response_context = self._resolve_dialogue_gate(request)
        if dialogue_decision.status != "ready_to_plan":
            raise ValueError(dialogue_decision.follow_up_question or "缺少必要规划字段")

        state.trace.append({"step": "dialogue_ready", "status": dialogue_decision.status, "response_mode": response_context.response_mode})

        self._emit_progress(progress, "正在检索景点、天气与住宿信息…")
        observation_log = self._run_planner_loop_with_tools(state, progress)
        if not state.attraction_result:
            attraction_observation = self._execute_tool_step(state, "attraction", progress)
            observation_log.append(attraction_observation)
            state.trace.append({"step": "tool_attraction", **attraction_observation})
        if not state.weather_result:
            weather_observation = self._execute_tool_step(state, "weather", progress)
            observation_log.append(weather_observation)
            state.trace.append({"step": "tool_weather", **weather_observation})
        if not state.lodging_result:
            lodging_observation = self._execute_tool_step(state, "lodging", progress)
            observation_log.append(lodging_observation)
            state.trace.append({"step": "tool_lodging", **lodging_observation})
        if not state.meal_result:
            meal_observation = self._execute_tool_step(state, "meal", progress)
            observation_log.append(meal_observation)
            state.trace.append({"step": "tool_meal", **meal_observation})
        budgets = PlanningBudgets()
        self._emit_progress(progress, "正在汇总分析候选景点…")
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

        self._emit_progress(progress, "正在编排每日行程框架…")
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

        # 骨架已定下每天景点分布：按天分别发起餐饮查询（agent 自主多次调用），替代单一全局餐饮池
        self._build_day_meal_pools(state.request, state, skeleton)
        state.trace.append(
            {
                "step": "tool_meal_per_day",
                "days": sorted(state.day_meal_pool.keys()),
                "pools": {k: len(v) for k, v in state.day_meal_pool.items()},
            }
        )

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
        self._emit_progress(progress, "正在生成每日行程详情…")
        state.draft = self._render_final_itinerary(state, skeleton, lodging_anchor)
        self._emit_progress(progress, "正在校验并完善行程…")
        state = self._converge_draft(state, budgets)
        draft = state.draft

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
            extract_plan_attractions(state.plan) if state.plan else [],
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

        app_logger.info(
            "planning_pipeline_done",
            duration_ms=int((_time.perf_counter() - _stage_start) * 1000),
            day_count=len(state.draft.day_plans),
            attraction_candidates=len(state.attraction_result.candidates) if state.attraction_result else 0,
            transport_routes=len(state.transport_results),
            selected_lodging=state.selected_lodging.name if state.selected_lodging else None,
            session_id=plan_input.session_id,
        )

        return {
            "planning_trace": state.trace,
            "final_state": final_state,
            "final_decision": final_decision,
            "final_draft": draft,
            "plan": state.plan,
        }

    # 只读字段校验兜底：对话阶段判定已在 orchestrator 侧由 SessionStateService 完成，
    # 这里仅在规划前做最后一道字段完整性检查（口径与 REQUIRED_FIELDS 单一来源一致），
    # 不修改任何状态。
    def _resolve_dialogue_gate(self, request: PlanningRequest) -> tuple[DialogueDecision, ResponseContext]:
        field_prompts = {
            "destination": "想去哪个目的地呢",
            "start_date": "计划哪天出发呢",
            "end_date": "计划玩到哪天呢",
        }
        missing = compute_missing_fields(request)
        if not missing:
            return (
                DialogueDecision(status="ready_to_plan"),
                ResponseContext(response_mode="final_plan"),
            )
        prompts = [field_prompts.get(f, f) for f in missing]
        question = ("还差最后一步：" + prompts[0] + "？") if len(prompts) == 1 else (
            "还差几个信息：" + "、".join(prompts) + "？"
        )
        return (
            DialogueDecision(status="need_clarification", missing_fields=missing, follow_up_question=question),
            ResponseContext(response_mode="follow_up", include_alternatives=False),
        )

    def _run_planner_loop(
        self,
        state: PlanningContext,
        progress: Callable[[str, dict], None] | None = None,
    ) -> list[dict]:
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
            tool_observation = self._execute_tool_step(state, tool_name, progress)
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

    def _run_planner_loop_with_tools(
        self,
        state: PlanningContext,
        progress: Callable[[str, dict], None] | None = None,
    ) -> list[dict]:
        """收集规划所需工具信息。

        优先由 LLM 通过 function calling 自主决定调用哪些/几次工具（含按片区拆分的餐饮），
        使其与后续骨架、渲染的编排更贴合，不再是最开始固定顺序的一次性收集。
        LLM 不可用或调用异常的时空降到固定顺序收集，保证后台稳定性。
        """
        llm_client = get_llm_client()
        if llm_client.is_enabled():
            observation_log = self._run_llm_tool_loop(state, progress)
            # 兜底：LLM 自主循环未收集到必要的动素时，补上缺失项
            if not state.attraction_result:
                observation_log.append(self._execute_tool_step(state, "attraction", progress))
            if not state.weather_result:
                observation_log.append(self._execute_tool_step(state, "weather", progress))
            if self._should_collect_lodging(state) and not state.lodging_result:
                observation_log.append(self._execute_tool_step(state, "lodging", progress))
            return observation_log
        return self._run_planner_loop(state, progress)

    def _run_llm_tool_loop(
        self,
        state: PlanningContext,
        progress: Callable[[str, dict], None] | None = None,
    ) -> list[dict]:
        """LLM 自主决定工具调用的多轮 function calling 循环。"""
        observation_log: list[dict] = []
        llm_client = get_llm_client()

        def execute(fn_name: str, arguments: dict) -> dict:
            obs = self._execute_tool_call(state, fn_name, arguments, progress)
            observation_log.append(obs)
            state.trace.append({"step": f"tool_{obs.get('tool', fn_name)}", **obs})
            return obs

        user_prompt = (
            f"目的地：{state.request.destination}；行程天数：{state.request.days} 天；"
            f"出行人：{ '、'.join(state.request.travelers) if state.request.travelers else '未指定'}。\n"
            f"必去景点：{'、'.join(state.request.must_visit_spots) or '未指定'}；"
            f"偏好：{'、'.join(state.request.preferences) or '未指定'}；"
            f"预算：{state.request.budget or '未指定'}。\n"
            "请调用合适的工具收集规划所需的真实信息；尽量在一轮里同时规划多个工具调用，收集够就直接收束，避免冗余轮次（餐饮建议按片区/按天拆分多次调用）。\n"
        )
        content = llm_client.generate_with_tools(
            system_prompt=PLAN_TOOL_COLLECTION_PROMPT,
            user_prompt=user_prompt,
            tools=self._planner_tool_schemas(),
            tool_choice="auto",
            max_rounds=4,
            execute_tool=execute,
        )
        observation_log.append(
            {
                "step": "planner_next_action",
                "status": "enough_to_plan" if content else "loop_failed",
                "next_tool": "none",
                "reason": "LLM 自主决策结束工具收集" if content else "LLM 工具循环异常，走固定顺序兜底",
                "missing_information": [],
            }
        )
        return observation_log

    def _planner_tool_schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "attraction_tool",
                    "description": "检索目标城市的候选景点素材。多数规划第一步调用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "目的地城市"},
                            "days": {"type": "integer", "description": "行程天数"},
                            "must_visit_spots": {"type": "array", "items": {"type": "string"}, "description": "必去景点"},
                            "avoid_spots": {"type": "array", "items": {"type": "string"}, "description": "不去的景点"},
                            "preferences": {"type": "array", "items": {"type": "string"}, "description": "游玩偏好"},
                        },
                        "required": ["city", "days"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "weather_tool",
                    "description": "查询行程日期内逐日天气，用于户外/室内安排。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "城市"},
                            "start_time": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                            "end_time": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
                        },
                        "required": ["city"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "lodging_tool",
                    "description": "检索符合预算偏好的住宿候选，用于住宿锚点判断。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "城市"},
                            "preferences": {"type": "array", "items": {"type": "string"}, "description": "住宿偏好"},
                            "spots": {"type": "array", "items": {"type": "string"}, "description": "希望靠近的景点"},
                            "top_n": {"type": "integer", "description": "返回数量"},
                        },
                        "required": ["city"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "meal_tool",
                    "description": "按某一片区/某几个景点检索就近真实餐馆。可按片区/按天拆分多次调用，保证每天三餐就近。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "城市"},
                            "preferences": {"type": "array", "items": {"type": "string"}, "description": "餐饮偏好"},
                            "spots": {"type": "array", "items": {"type": "string"}, "description": "要就近的景点/片区名"},
                            "top_n": {"type": "integer", "description": "返回数量"},
                        },
                        "required": ["city"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "transport_tool",
                    "description": "查询两点间通行方案。通常在形成骨架后按需补充，收集阶段不必调用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "城市"},
                            "from_name": {"type": "string", "description": "起点"},
                            "to_name": {"type": "string", "description": "终点"},
                        },
                        "required": ["city", "from_name", "to_name"],
                    },
                },
            },
        ]

    def _execute_tool_call(
        self,
        state: PlanningContext,
        name: str,
        arguments: dict,
        progress: Callable[[str, dict], None] | None = None,
    ) -> dict:
        """执行一次 function calling 工具调用，并把结果写回 state"""
        label = _TOOL_STAGE_LABELS.get(name, f"正在调用 {name} 工具…")
        self._emit_progress(progress, label)
        request = state.request
        if name == "weather_tool":
            state.weather_result = self._run_weather(
                request,
                start_date=arguments.get("start_time"),
                end_date=arguments.get("end_time"),
            )
            return {
                "tool": "weather",
                "days": len(state.weather_result.daily) if state.weather_result else 0,
                "has_error": bool(state.weather_result.error) if state.weather_result else True,
            }
        if name == "attraction_tool":
            state.attraction_result = attraction_tool.run(
                AttractionInput(
                    city=arguments.get("city") or request.destination,
                    days=int(arguments.get("days") or request.days),
                    must_visit_spots=list(arguments.get("must_visit_spots") or request.must_visit_spots or []),
                    avoid_spots=list(arguments.get("avoid_spots") or request.avoid_spots or []),
                    preferences=list(arguments.get("preferences") or request.preferences or []),
                    target_count=arguments.get("target_count"),
                    target_count_min=arguments.get("target_count_min"),
                    target_count_max=arguments.get("target_count_max"),
                )
            )
            return {
                "tool": "attraction",
                "candidate_count": len(state.attraction_result.candidates) if state.attraction_result else 0,
                "must_visit_verified": len(state.attraction_result.must_visit_verified) if state.attraction_result else 0,
            }
        if name == "lodging_tool":
            result = lodging_tool.run(
                LodgingInput(
                    destination=arguments.get("city") or request.destination,
                    preferences=list(arguments.get("preferences") or request.preferences or []),
                    avoid_keywords=list(arguments.get("avoid_keywords") or list(request.avoid_spots or []) + ["招待所"]),
                    spots=list(arguments.get("spots") or []),
                    top_n=int(arguments.get("top_n") or 5),
                )
            )
            state.lodging_result = result
            state.selected_lodging = self._pick_selected_lodging(result.candidates)
            return {
                "tool": "lodging",
                "candidate_count": len(result.candidates),
                "selected_lodging": state.selected_lodging.name if state.selected_lodging else None,
            }
        if name == "meal_tool":
            state.meal_result = meal_tool.run(
                MealInput(
                    destination=arguments.get("city") or request.destination,
                    preferences=list(arguments.get("preferences") or _extract_meal_preferences(request)),
                    spots=list(arguments.get("spots") or []),
                    top_n=int(arguments.get("top_n") or 10),
                )
            )
            return {
                "tool": "meal",
                "candidate_count": len(state.meal_result.candidates) if state.meal_result else 0,
            }
        if name == "transport_tool":
            return {
                "tool": "transport",
                "route_count": len(state.transport_results),
                "status": "deferred_until_skeleton",
            }
        return {"tool": name, "status": "skipped"}

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
        # 空候选局部降级：没有可编排的候选时不空跑 LLM，直接走规则兜底
        # （无兜底时抛 PlanningDegradedError，由调用方转成可读引导消息）
        candidates = [item for item in (state.attraction_result.candidates if state.attraction_result else [])]
        if not candidates:
            fallback = self._build_fallback_cluster_plan(state)
            if fallback:
                state.trace.append({"step": "cluster_plan_fallback", "reason": "no_candidates"})
                return fallback
            raise PlanningDegradedError(
                "没能检索到可规划的景点，请补充想去的景点或换个目的地后重试。",
                detail="cluster_plan unavailable: attraction candidates empty",
            )

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
            fallback = self._build_fallback_cluster_plan(state)
            if fallback:
                state.trace.append({"step": "cluster_plan_fallback", "reason": "llm_parse_failed"})
                return fallback
            raise PlanningDegradedError(
                "暂时没能主题规划出可选方案，请稍后重试或换个偏好/目的地。",
                detail=f"cluster_plan unavailable: {llm_client.last_debug_info}",
            )
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
                raise PlanningDegradedError(
                    "行程主题调整遇到问题，请稍后重试或换个偏好/目的地。",
                    detail=f"skeleton unavailable: {llm_client.last_debug_info}",
                )
        skeleton = self._canonicalize_skeleton_spots(state, skeleton)
        if not self._is_skeleton_within_candidates(state, skeleton):
            # 骨架含候选池外景点时，降级到规则骨架（LLM 可自愈时不崩链路）
            fallback = self._build_fallback_skeleton_from_clusters(state, cluster_plan)
            if fallback:
                state.trace.append({"step": "skeleton_fallback", "reason": "spots_outside_candidates"})
                skeleton = fallback
            else:
                raise PlanningDegradedError(
                    "行程主题调整遇到底层候选问题，请稍后重试或换个目的地/偏好。",
                    detail="skeleton spots outside candidates, fallback unavailable",
                )
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
        state.selected_lodging = self._pick_selected_lodging(state.lodging_result.candidates if state.lodging_result else None)
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
        def _query(target):
            return transport_tool.run(
                TransportInput(
                    city=state.request.destination,
                    from_name=target.from_label,
                    to_name=target.to_label,
                )
            )

        targets = request.targets[:3]
        routes: list = []
        if len(targets) > 1:
            with ThreadPoolExecutor(max_workers=min(len(targets), 4)) as _ex:
                for future in as_completed([_ex.submit(_query, t) for t in targets]):
                    routes.extend(future.result())
        else:
            for t in targets:
                routes.extend(_query(t))
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
        kb_details, kb_guide = self._load_kb_render_context(state)
        payload = FinalItineraryRenderInput(
            request=state.request.model_dump(),
            skeleton=skeleton.model_dump(),
            weather=[item.model_dump() for item in (state.weather_result.daily if state.weather_result else [])],
            attraction_candidates=[item.model_dump() for item in (state.attraction_result.candidates if state.attraction_result else [])],
            lodging_candidates=[item.model_dump() for item in (state.lodging_result.candidates if state.lodging_result else [])],
            selected_lodging=state.selected_lodging.model_dump() if state.selected_lodging else None,
            meal_candidates=[item.model_dump() for item in (state.meal_result.candidates if state.meal_result else [])],
            planning_anchor=lodging_anchor.model_dump(),
            transport_evidence=[item.model_dump() for item in state.transport_results],
            kb_attraction_details=kb_details,
            city_kb_guide=kb_guide,
        )

        days = list(skeleton.day_skeletons)
        # 单天：直接复用整段渲染（与旧逻辑一致，响应一次成功）
        if len(days) <= 1:
            return self._render_one_day(state, payload, day_index=None, llm_client=llm_client)

        # 多天：按天并行渲染，避免一次全量生成成为 100s 级瓶颈
        return self._render_all_days_parallel(state, payload, days, llm_client=llm_client)

    def _render_one_day(
        self,
        state: PlanningContext,
        payload: FinalItineraryRenderInput,
        day_index: int | None,
        llm_client,
    ) -> ItineraryDraftSchema:
        """渲染单个文档：day_index=None 表示整篇（skeleton 已含全部天），否则只渲染该天."""
        prompt = build_itinerary_render_prompt(payload, only_day=day_index)
        draft = llm_client.generate_itinerary_draft(system_prompt=self._render_prompt, user_prompt=prompt)
        if draft:
            return draft
        retry_prompt = prompt + "\n\n[render_retry_instruction]\n请缩短 detail 与 notes，优先保证输出完整、合法、可解析的 JSON。若不确定，请使用更短的 detail，但不要省略晚餐与晚间正式时段，也不要把一天在18:30–19:30就提前收掉。"
        draft = llm_client.generate_itinerary_draft(system_prompt=self._render_prompt, user_prompt=retry_prompt)
        if draft:
            return draft
        raise PlanningDegradedError(
            "行程生成遇到问题，请稍后重试，或换个目的地/日期后再试。",
            detail=f"render failed: {llm_client.last_debug_info}",
        )

    def _render_all_days_parallel(
        self,
        state: PlanningContext,
        payload: FinalItineraryRenderInput,
        days,
        llm_client,
    ) -> ItineraryDraftSchema:
        """多天并行渲染后合并：每个 worker 渲染自己那一天，最后按 day_index 收敛成一个完整行程稿。"""
        def _single_skeleton_payload(day) -> FinalItineraryRenderInput:
            sk_dict = dict(payload.skeleton)
            sk_dict["day_skeletons"] = [
                d.model_dump() for d in [day] if getattr(d, "day_index", None) == day.day_index
            ]
            return payload.model_copy(update={"skeleton": sk_dict})

        def _render_day(day) -> ItineraryDraftSchema | None:
            try:
                return self._render_one_day(
                    state, _single_skeleton_payload(day), day_index=day.day_index, llm_client=llm_client
                )
            except PlanningDegradedError:
                return None

        day_results: dict[int, ItineraryDraftSchema] = {}
        # worker 数随机天数走：1 天串行（根本不会走到这），2-4 天各自并行，5 天以上也封顶 4，
        # 避免对 dashscope 的并发请求过高触发限流，导致个别天失败反而退回整篇渲染
        failed: list[int] = []
        app_logger.info(
            "render_parallel_start",
            days=len(days),
            workers=min(len(days), 4),
            day_indices=[d.day_index for d in days],
        )
        with ThreadPoolExecutor(max_workers=min(len(days), 4)) as ex:
            future_map = {ex.submit(_render_day, d): d.day_index for d in days}
            for fut in as_completed(future_map):
                day_index = future_map[fut]
                try:
                    draft = fut.result()
                except Exception as _exc:  # `_render_day` 内已接住 PlanningDegradedError，这里兜底其它异常
                    failed.append(day_index)
                    app_logger.error("render_parallel_day_error", day_index=day_index, err=repr(_exc))
                    continue
                if draft and draft.day_plans:
                    day_results[day_index] = draft
                else:
                    failed.append(day_index)

        if failed:
            # 有某天渲染失败：退回整篇渲染，保底不丢行程，并明确抛出回退原因便于排查
            app_logger.warning(
                "render_parallel_fallback",
                days=len(days),
                ok=len(day_results),
                failed_days=failed,
                reason="个别天并行渲染失败",
            )
            return self._render_one_day(state, payload, day_index=None, llm_client=llm_client)

        app_logger.info("render_parallel_done", days=len(days), day_indices=sorted(day_results.keys()))

        summaries = [d.summary or "" for _, d in sorted(day_results.items())]
        merged = ItineraryDraftSchema(
            destination=state.request.destination,
            summary="；".join(dict.fromkeys(s for s in summaries if s)) or "",
            route_intent_summary=None,
            selected_day_areas=[
                dp.primary_area
                for dp in (d.day_plans[0] for _, d in sorted(day_results.items()))
                if dp.primary_area
            ],
            day_plans=[d.day_plans[0] for _, d in sorted(day_results.items())],
        )
        return merged

    def _load_kb_render_context(self, state: PlanningContext) -> tuple[list[dict], list[str]]:
        """渲染前从知识库拉取该城市景点细节与攻略段，替代高德短字段；任何异常都静默降级为空列表。"""
        details: list[dict] = []
        guide: list[str] = []
        try:
            city = state.request.destination
            spot_names: list[str] = []
            if state.attraction_result:
                for c in state.attraction_result.candidates:
                    n = (getattr(c, "title", None) or getattr(c, "name", "") or "").strip()
                    if n:
                        spot_names.append(n)
            kb_items = knowledge_service.get_all(ATTRACTION_COLLECTION, where={"city": city})
            by_name: dict[str, str] = {}
            for it in kb_items:
                meta = it.metadata or {}
                by_name[(meta.get("name") or "").strip()] = it.text or ""
            seen: set[str] = set()
            for name in spot_names:
                text = by_name.get(name) or by_name.get(name.split("（")[0].strip())
                if text and name not in seen:
                    details.append({"name": name, "text": text})
                    seen.add(name)
            result = knowledge_service.retrieve(QA_COLLECTION, f"{city} 旅行攻略 推荐路线 美食", top_k=4)
            guide_seen: set[str] = set()
            for it in (result.items if result else []):
                t = (it.text or "").strip()
                if t and t not in guide_seen:
                    guide.append(t)
                    guide_seen.add(t)
        except Exception:  # noqa: BLE001 知识库不可用/未入库时静默降级
            return [], []
        return details, guide[:6]

    def _converge_draft(self, state: PlanningContext, budgets: PlanningBudgets) -> PlanningContext:
        """收敛式校验修复：先分日并行收敛（Orchestrator-Workers），再全局一致性 pass
        与 revise 复用同一套 reflection + repair，避免"同类问题一处修一处崩"
        """
        # Phase 4：统一收敛预算，各阶段共享，LLM 修复与规则修复分开计量
        budget = ConvergenceBudget(
            llm_rounds=max(int(budgets.render_repair_remaining), 1),
            rule_rounds=max(int(budgets.render_repair_remaining), 1),
        )
        # 阶段一：分日并行收敛，各天在独立 state 副本上收敛，互不阻塞
        day_indices = [day.day_index for day in state.draft.day_plans]
        converged, repairs, day_observations = self._converge_days_parallel(state, day_indices, budget)
        state.revision_count += repairs
        for obs in day_observations:
            state.trace.append({"step": "converge_observe", **obs})
        # 合并各天结果后统一收口（天气备注/块结构/跨天去重/摘要）
        day_map = {day.day_index: day for day in state.draft.day_plans}
        for day_index, new_day in converged.items():
            day_map[day_index] = new_day
        state.draft.day_plans = [day_map[idx] for idx in sorted(day_map)]
        state = planning_repair.finalize_draft(state)
        # 阶段二：全局一致性 pass（Phase 3），处理跨天/全局不变式并定向回修
        state = self._converge_global_pass(state, budget)
        state.trace.append(
            {
                "step": "plan_converge",
                "reflection_status": state.reflection_result.status if state.reflection_result else None,
                "revision_count": state.revision_count,
                "day_count": len(day_indices),
            }
        )
        if not state.draft or not state.draft.day_plans:
            raise RuntimeError("Failed to converge itinerary draft.")
        return state

    def _record_observation(
        self,
        state: PlanningContext,
        *,
        phase: str,
        round_no: int,
        status: str,
        issue_codes: list[str] | None = None,
        action: str | None = None,
        budget_used: int = 0,
    ) -> None:
        """每阶段观测日志：记录一次评审-修复迭代的结论与预算消耗，写入 state.trace"""
        state.trace.append(
            {
                "step": "converge_observe",
                "phase": phase,
                "round": round_no,
                "status": status,
                "issues": list(issue_codes or []),
                "action": action,
                "budget_used": budget_used,
            }
        )

    def _converge_global_pass(self, state: PlanningContext, budget: ConvergenceBudget) -> PlanningContext:
        """Phase 3 全局一致性 pass：跨天/全局不变式定向收敛（受 Phase 4 统一预算约束）

        分日并行（Phase 2）已收敛天内部（daily_plan）不变式；此阶段聚焦跨天不变式：
        必去景点覆盖、跨天去重、天气跨天分流、候选/住宿/天气可用性等。修复依赖
        reflection 产出的 fix_hint 携带 day_index（best_fit_day/受影响天），
        由 repair 定向重建受影响的天，避免全量 rebuild 扰动已收敛的其它天。
        LLM 修复（API 成本）受 llm_rounds 限制，超出后回退规则式修复（rule_rounds）。
        """
        llm_enabled = get_llm_client().is_enabled()
        # 1) 先用廉价的规则复检做前置判断：行程已满足全部跨天/全局不变式时，直接放行，
        #    省掉整轮 LLM 评审（原先无论如何都会先调一次 LLM review）。
        rule_view = planning_reflection.rule_review(state)
        rule_codes = [issue.code for issue in rule_view.issues]
        if rule_view.status != "revise":
            state.reflection_result = rule_view
            self._record_observation(state, phase="global_pass", round_no=1, status="accept", issue_codes=rule_codes, budget_used=0)
            return state

        rounds = 0
        # 2) 规则查出跨天问题：预算内做一次 LLM 定向修复（评审+修复一步到位），
        #    无需像原先那样 review→repair→review 三次串行 LLM。
        if budget.llm_rounds > 0 and llm_enabled:
            state.reflection_result = planning_reflection.review(state)
            llm_codes = [issue.code for issue in state.reflection_result.issues]
            if state.reflection_result.status != "revise":
                self._record_observation(state, phase="global_pass", round_no=rounds + 1, status="accept", issue_codes=llm_codes, budget_used=1)
                return state
            state = planning_repair.repair(state)
            rounds += 1
            self._record_observation(state, phase="global_pass", round_no=rounds, status="revise", issue_codes=llm_codes, action="repair_llm", budget_used=1)
        else:
            state = planning_repair.rule_based_repair(state)
            rounds += 1
            self._record_observation(state, phase="global_pass", round_no=rounds, status="revise", issue_codes=rule_codes, action="repair_rule", budget_used=1)

        # 3) 收尾：用零成本的规则复检验证修复结果，不再追加一次 LLM 复查。
        state.reflection_result = planning_reflection.rule_review(state)
        self._record_observation(
            state, phase="global_pass", round_no=rounds + 1,
            status=state.reflection_result.status,
            issue_codes=[issue.code for issue in state.reflection_result.issues],
            action="verify_rule", budget_used=rounds + 1,
        )
        return state

    def _converge_days_parallel(
        self,
        state: PlanningContext,
        day_indices: list[int],
        budget: ConvergenceBudget,
    ) -> tuple[dict[int, ItineraryDayPlan], int, list[dict]]:
        """分日并行收敛：ThreadPoolExecutor 并发跑单天 worker，返回各天 day_plan、总修复次数与观测日志"""
        if not day_indices:
            return {}, 0, []
        workers = min(len(day_indices), os.cpu_count() or 1)
        converged: dict[int, ItineraryDayPlan] = {}
        total_repairs = 0
        observations: list[dict] = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self._converge_day_worker, state, day_index, budget): day_index
                for day_index in day_indices
            }
            for future in as_completed(futures):
                day_index = futures[future]
                new_day, repairs, obs = future.result()
                converged[day_index] = new_day
                total_repairs += repairs
                observations.extend(obs)
        return converged, total_repairs, observations

    def _converge_day_worker(
        self,
        state: PlanningContext,
        day_index: int,
        budget: ConvergenceBudget,
    ) -> tuple[ItineraryDayPlan, int, list[dict]]:
        """单天并发 worker：在独立 state 副本上收敛指定天（daily_plan 域，规则式）

        只重建目标天、不做整篇 finalize/去重（去重由合并后的统一 finalize 处理），
        返回最终 day_plan、本天修复次数与本天观测日志，供编排层合并
        """
        work = state.model_copy(deep=True)
        repairs = 0
        observations: list[dict] = []
        for round_no in range(budget.rule_rounds):
            result = planning_reflection.review(work, scopes={"daily_plan"}, days={day_index})
            work.reflection_result = result
            observations.append(
                {
                    "phase": f"day_{day_index}",
                    "round": round_no + 1,
                    "status": result.status,
                    "issues": [issue.code for issue in result.issues],
                    "action": "rebuild_day" if result.status == "revise" else None,
                    "budget_used": repairs,
                }
            )
            if result.status != "revise":
                break
            work.draft.day_plans = [
                planning_repair.rebuild_day(work, day_index) if day.day_index == day_index else day
                for day in work.draft.day_plans
            ]
            repairs += 1
        final_day = next(day for day in work.draft.day_plans if day.day_index == day_index)
        return final_day, repairs, observations

    def _compose_plan(self, state: PlanningContext, skeleton: PlanningSkeleton, lodging_anchor: LodgingAnchorDecision) -> TripPlan:
        draft = state.draft
        request = state.request
        lodging_candidates = state.lodging_result.candidates if state.lodging_result else []
        weather_days = state.weather_result.daily if state.weather_result else []
        destination = request.destination
        poi_index = self._build_poi_index(state)

        daily_plan = []
        meal_pool = [
            {"name": c.name, "area": c.area, "lng": c.lng, "lat": c.lat}
            for c in (state.meal_result.candidates if state.meal_result else [])
            if c.lng is not None
        ]
        used_meal: set[str] = set()
        lodging_name = state.selected_lodging.name if state.selected_lodging else None
        place_cache: dict[str, tuple[float, float]] = {}

        def _resolve_coord(title: str | None):
            """按标题解析真实坐标：优先打中候选池（poi_index），否则高德 geocode 兜底，
            再否则用 POI 文本搜索兜底（面状/知名地标（如北京路步行街）普通 geocode 常反解失败）。"""
            raw = str(title or "").strip()
            if not raw:
                return None
            # 缓存键用原始标题（去重），解析主体用清理后的地名
            cached = place_cache.get(raw)
            if cached:
                return cached
            # 两种必然失败的浪费请求直接跳过：
            # 1) 泛称餐饮（午餐/晚餐/休整等）无具体店址，geocode/place 都反解不出
            # 2) 复合路线串（从A前往B / A→B），不是单一地点，整串反解既错又慢
            if self._is_generic_meal_title(raw):
                return None
            if re.search(r"→|前往|返回到|^从.{1,40}(前往|到|返回)", raw):
                return None
            clean = self._clean_route_verb(raw)
            poi = poi_index.get(self._normalize_poi_name(clean))
            if poi and poi.get("lng") is not None:
                return poi["lng"], poi["lat"]
            lng, lat = self._geocode_point(clean, destination)
            if lng is not None and lat is not None:
                return lng, lat
            loc = None
            try:
                if amap_client.is_enabled():
                    pois = amap_client.search_pois(keywords=clean, city=destination, city_limit=True)
                    for p in pois or []:
                        if p.get("lng") is not None and p.get("lat") is not None:
                            loc = (p["lng"], p["lat"])
                            break
            except Exception:
                loc = None
            if loc:
                place_cache[raw] = loc
            return loc

        # 预取所有转场/进店的真实路线：先解析坐标对去重，再并行请求高德，避免逐块串行叠加耗时
        pending: dict[tuple[float, float, float, float], tuple] = {}
        for day in draft.day_plans:
            day_blocks = self._clean_block_times(day.time_blocks)
            for bidx, blk in enumerate(day_blocks):
                if blk.item_type not in {"transport", "return", "meal"}:
                    continue
                p_activity = n_activity = None
                for j in range(bidx - 1, -1, -1):
                    if day_blocks[j].item_type in {"attraction", "meal", "flex"}:
                        p_activity = day_blocks[j].title
                        break
                if blk.item_type in {"transport", "return"}:
                    for j in range(bidx + 1, len(day_blocks)):
                        if day_blocks[j].item_type in {"attraction", "meal", "flex"}:
                            n_activity = day_blocks[j].title
                            break
                f_coord = _resolve_coord(p_activity) or _resolve_coord(lodging_name)
                t_coord = _resolve_coord(n_activity) or _resolve_coord(blk.title) if blk.item_type in {"transport", "return"} else _resolve_coord(blk.title)
                if f_coord and t_coord:
                    key = (f_coord[0], f_coord[1], t_coord[0], t_coord[1])
                    pending.setdefault(key, (f_coord, t_coord))
        route_cache: dict = {}
        if pending:
            with ThreadPoolExecutor(max_workers=min(len(pending), max(4, os.cpu_count() or 4))) as ex:
                futures = {ex.submit(self._fetch_transport_route, destination, ft[0], ft[1]): key for key, ft in pending.items()}
                for fut in as_completed(futures):
                    route_cache[futures[fut]] = fut.result()

        def _route_for(from_coord, to_coord):
            """命中并行预取缓存则复用；进店因餐馆名回填可能出现坐标对差异，未命中时懒加载一次。"""
            if not from_coord or not to_coord:
                return None
            key = (from_coord[0], from_coord[1], to_coord[0], to_coord[1])
            if key not in route_cache:
                route_cache[key] = self._fetch_transport_route(destination, from_coord, to_coord)
            return route_cache.get(key)

        def _alt_coord(rec: dict | None):
            """取某活动块坐标；缺失时用标题按高德 place 搜索兜底补齐（如北京路这类面状地标）。"""
            if not rec:
                return None
            if rec.get("lng") is not None and rec.get("lat") is not None:
                return rec.get("lng"), rec.get("lat")
            c = _resolve_coord(rec.get("title"))
            if c:
                rec["lng"], rec["lat"] = c
            return c

        def _ensure_transport_alternation(its: list[dict]) -> list[dict]:
            """交替兜底：相邻两个非交通活动块之间若缺交通块，插入一条转场栏（端点缺坐标会先补齐），
            保证「景点-交通-餐饮-交通-景点…」的结构性交替；复用路线缓存，避免无谓的高德调用。"""
            filled: list[dict] = []
            prev: dict | None = None
            injected = 0
            for it in its:
                if it.get("item_type") in ("transport", "return"):
                    filled.append(it)
                    prev = None
                    continue
                if prev is not None and injected < 5:
                    from_c = _alt_coord(prev)
                    to_c = _alt_coord(it)
                    if from_c and to_c:
                        from_lng, from_lat = from_c
                        to_lng, to_lat = to_c
                        route = _route_for(from_c, to_c)
                        p_title = str(prev.get("title") or "上一站")
                        n_title = str(it.get("title") or "下一站")
                        gap: dict = {
                            "title": f"{p_title} → {n_title}",
                            "start_time": prev.get("end_time") or it.get("start_time"),
                            "end_time": it.get("start_time"),
                            "item_type": "transport",
                            "from_lng": from_lng,
                            "from_lat": from_lat,
                            "to_lng": to_lng,
                            "to_lat": to_lat,
                            "lng": to_lng,
                            "lat": to_lat,
                        }
                        if route:
                            gap["transport"] = route
                            gap["path"] = self._downsample_path(route.get("path"))
                            gap["detail"] = self._transition_route_text(route, p_title, n_title)
                        else:
                            # 兜底补交通栏但无法拉取真实路线：标题已含起终点，不再冗余显示"从A前往B"
                            gap["detail"] = ""
                        filled.append(gap)
                        injected += 1
                filled.append(it)
                prev = it
            return filled

        for day in draft.day_plans:
            blocks = self._clean_block_times(day.time_blocks)
            items = []
            # 优先用该天景点分布查出的按天餐饮池；缺失时回退到全局池
            meal_pool = state.day_meal_pool.get(day.day_index) or meal_pool
            for idx, block in enumerate(blocks):
                is_transition = block.item_type in {"transport", "return"}
                # 相邻真实活动（用于回填真实转场起终点与转乘信息）
                prev_activity = next_activity = None
                if is_transition or block.item_type == "meal":
                    for j in range(idx - 1, -1, -1):
                        if blocks[j].item_type in {"attraction", "meal", "flex"}:
                            prev_activity = blocks[j].title
                            break
                    for j in range(idx + 1, len(blocks)):
                        if blocks[j].item_type in {"attraction", "meal", "flex"}:
                            next_activity = blocks[j].title
                            break

                poi = poi_index.get(self._normalize_poi_name(block.title))
                item = {
                    "title": self._normalize_block_title(block.title, block.item_type),
                    "start_time": block.start_time,
                    "end_time": block.end_time,
                    "area": block.area,
                    "detail": self._normalize_candidate_detail(block.detail, block.area),
                    "item_type": block.item_type,
                }
                if block.item_type == "attraction":
                    # 景点：长介绍压到最前 2 个要点，避免一整段堆叠
                    item["detail"] = self._shorten_attraction_detail(item["detail"])
                if block.item_type == "meal":
                    if meal_pool and self._is_generic_meal_title(block.title):
                        # 就近选餐馆：同时兼顾上一景点与下一景点，避免选得偏离转场路径太远
                        prev_coord = _resolve_coord(prev_activity) or _resolve_coord(lodging_name)
                        next_coord = _resolve_coord(next_activity)
                        chosen = self._pick_meal_candidate(
                            meal_pool, used_meal, prev_coord, next_coord, block.area, day.primary_area
                        )
                        if chosen:
                            item["title"] = chosen["name"]
                            if chosen.get("area"):
                                item["area"] = chosen["area"]
                            if chosen.get("lng") is not None and chosen.get("lat") is not None:
                                item["lng"], item["lat"] = chosen["lng"], chosen["lat"]
                            poi = {"lng": chosen.get("lng"), "lat": chosen.get("lat")}
                    # 进店转乘已由前一个"交通"块单独呈现（交替兜底会在景点→餐前插入交通栏），
                    # 餐饮块不再重复该路线文案，避免"打车/步行"同时出现在交通栏与餐饮栏
                    item["detail"] = self._strip_meal_boilerplate(item.get("detail"))
                if poi and poi.get("poi_id"):
                    item["poi_id"] = poi["poi_id"]
                if poi and poi.get("address"):
                    item["address"] = poi["address"]
                lng, lat = (poi or {}).get("lng"), (poi or {}).get("lat")
                if is_transition:
                    # 转场/返程：起终点都解析为真实坐标；返程终点固定是酒店，保证每条转场都有时长
                    is_return = block.item_type == "return"
                    to_title = lodging_name if is_return else (next_activity or block.title)
                    from_title = prev_activity or lodging_name
                    # from 坐标优先沿用上一个已解析非交通块的真实坐标（避免按标题重解析失败/错位）
                    from_coord = None
                    for _it in reversed(items):
                        if _it.get("item_type") in ("attraction", "meal", "flex") and _it.get("lng") is not None and _it.get("lat") is not None:
                            from_coord = (_it["lng"], _it["lat"])
                            break
                    if from_coord is None:
                        from_coord = _resolve_coord(from_title)
                    to_coord = _resolve_coord(to_title)
                    if from_coord:
                        item["from_lng"], item["from_lat"] = from_coord
                    if to_coord:
                        item["to_lng"], item["to_lat"] = to_coord
                        # 交通块同样要带 lng/lat，否则前端地图按 lng/lat 过滤会把它和它的真实轨迹整条丢弃
                        item["lng"], item["lat"] = to_coord
                        lng, lat = to_coord
                    elif from_coord:
                        item["lng"], item["lat"] = from_coord
                    route = _route_for(from_coord, to_coord)
                    if route:
                        item["transport"] = route
                        item["path"] = self._downsample_path(route["path"])
                        item["detail"] = self._transition_route_text(route, from_title, to_title)
                    else:
                        # 路线解析失败：剥掉"建议优先地铁…控制出发时间…"等泛称套话；
                        # 若仍只剩"从A前往B"这类与标题重复的空壳，则不再冗余展示（标题已表达起终点）。
                        stripped = self._strip_markers(item.get("detail"), self._TRANSITION_BOILERPLATE_MARKERS)
                        if stripped and any(token in stripped for token in ("分钟", "公里")):
                            item["detail"] = stripped
                        else:
                            item["detail"] = ""
                elif (lng is None or lat is None) and block.item_type == "attraction":
                    lng, lat = self._geocode_point(block.title, destination)
                if lng is not None and lat is not None:
                    item["lng"] = lng
                    item["lat"] = lat
                if self._is_leaked_id_title(item.get("title")):
                    continue  # 过滤掉标题泄露为高德 poi_id 的垃圾块（如 "B0FFH6K3XY"）
                items.append(item)
            if len(items) > 1:
                items = _ensure_transport_alternation(items)
            daily_plan.append(
                {
                    "day_index": day.day_index,
                    "primary_area": day.primary_area,
                    "items": items,
                    "notes": self._normalize_day_notes(day.notes),
                }
            )

        # 补齐每天餐饮：#4 规则兜底 —— LLM 渲染偶发漏掉午餐/晚餐时，就近补一个真实餐馆块，保证一致
        def _hour(t):
            try:
                return int(str(t).split(":")[0])
            except (ValueError, AttributeError, TypeError):
                return None

        for dy in daily_plan:
            its = dy["items"]
            day_pool = state.day_meal_pool.get(dy.get("day_index")) or meal_pool
            meal_starts = [
                _hour(it.get("start_time")) for it in its if it.get("item_type") == "meal"
            ]
            meal_starts = [h for h in meal_starts if h is not None]
            has_lunch = any(11 <= h < 15 for h in meal_starts)
            has_dinner = any(17 <= h < 23 for h in meal_starts)
            day_ends = [
                _hour(it.get("end_time"))
                for it in its
                if it.get("item_type") in ("attraction", "flex", "meal")
            ]
            day_ends = [h for h in day_ends if h is not None]
            day_end = max(day_ends) if day_ends else None
            if not day_pool:
                continue
            # 只在对应时段存在真实空闲档时才补餐，避免与景点/返程时间重叠、无编排地硬塞
            def _to_min(s):
                try:
                    h, m = str(s).split(":")
                    return int(h) * 60 + int(m)
                except (ValueError, AttributeError, TypeError):
                    return None

            def _fmt(m):
                return f"{m // 60:02d}:{m % 60:02d}"

            def _find_meal_slot(ws_h, we_h, dur_min=75):
                """在 [ws_h, we_h) 时段内找一段不与当天已有块重叠、长度>=dur_min 的空档，返回 (起点,终点) 或 None。"""
                occ = []
                for it in its:
                    s, e = _to_min(it.get("start_time")), _to_min(it.get("end_time"))
                    if s is None or e is None or e <= s:
                        continue
                    # 交通/返程块也计入已占用，避免补餐时段与转场重叠、插进去却和交通撞车
                    if it.get("item_type") in ("attraction", "meal", "flex", "return", "transport"):
                        occ.append((s, e))
                if not occ:
                    return None
                occ.sort()
                win_s, win_e = ws_h * 60, we_h * 60
                cur = win_s
                for (s, e) in occ:
                    if e <= cur:
                        continue
                    if s > cur and s - cur >= dur_min:
                        return (_fmt(cur), _fmt(cur + dur_min))
                    cur = max(cur, e)
                    if cur >= win_e:
                        break
                if win_e - cur >= dur_min:
                    return (_fmt(cur), _fmt(cur + dur_min))
                return None

            def _free_dinner_slot(its):
                """保证晚餐：先找不重叠的 ≥60 分钟空档；实在没有时（天被排满），
                去掉最晚结束的晚间景点及其前一段交通，腾出空档，返回 (slot, 裁剪后的列表) 或 (None, its)。"""
                slot = _find_meal_slot(17, 21, 60)
                if slot:
                    return slot, its
                victim = None
                for it in its:
                    if it.get("item_type") == "attraction" and (_to_min(it.get("end_time")) or 0) >= 18 * 60:
                        if victim is None or (_to_min(it.get("end_time")) or 0) > (_to_min(victim.get("end_time")) or 0):
                            victim = it
                if victim is None:
                    return None, its
                vc = (victim.get("lng"), victim.get("lat"))
                kept = []
                for it in its:
                    if it is victim:
                        continue
                    if it.get("item_type") == "transport" and vc:
                        tc = (it.get("to_lng"), it.get("to_lat"))
                        if tc and abs(tc[0] - vc[0]) < 1e-6 and abs(tc[1] - vc[1]) < 1e-6:
                            continue
                    kept.append(it)
                slot = _find_meal_slot(17, 21, 60)
                return slot, kept

            additions = []
            if not has_lunch and (day_end is None or day_end >= 11):
                slot = _find_meal_slot(11, 14, 75)
                if slot:
                    additions.append(("午餐", slot[0], slot[1]))
            if not has_dinner and (day_end is not None and day_end >= 17):
                slot = _find_meal_slot(17, 21, 60)
                if slot is None:
                    slot, its = _free_dinner_slot(its)
                    if slot:
                        dy["items"][:] = its
                if slot:
                    additions.append(("晚餐", slot[0], slot[1]))
            for label, st, et in additions:
                st_min, et_min = _to_min(st), _to_min(et)
                anchors = [it for it in its if it.get("lng") is not None and it.get("item_type") in ("attraction", "meal")]
                prev_a = nxt_a = None
                for it in sorted(anchors, key=lambda a: _to_min(a.get("end_time")) or 0):
                    if (_to_min(it.get("end_time")) or 0) <= st_min:
                        prev_a = it
                for it in sorted(anchors, key=lambda a: _to_min(a.get("start_time")) or 99):
                    if (_to_min(it.get("start_time")) or 99) >= et_min:
                        nxt_a = it
                p_coord = (prev_a.get("lng"), prev_a.get("lat")) if prev_a else _resolve_coord(lodging_name)
                n_coord = (nxt_a.get("lng"), nxt_a.get("lat")) if nxt_a else None
                chosen = self._pick_meal_candidate(
                    day_pool, used_meal, p_coord, n_coord, dy.get("primary_area"), dy.get("primary_area")
                )
                if not chosen:
                    continue
                title = chosen["name"]
                lng, lat = chosen.get("lng"), chosen.get("lat")
                item = {
                    "title": title,
                    "start_time": st,
                    "end_time": et,
                    "area": chosen.get("area") or dy.get("primary_area"),
                    "item_type": "meal",
                }
                if lng is not None and lat is not None:
                    item["lng"], item["lat"] = lng, lat
                from_coord = p_coord
                to_coord = (lng, lat) if lng is not None and lat is not None else _resolve_coord(title)
                route = _route_for(from_coord, to_coord)
                if route:
                    item["transport"] = route
                    item["path"] = self._downsample_path(route["path"])
                    origin_name = prev_a.get("title") if prev_a else (lodging_name or "住处")
                    item["detail"] = f"补充{label}：从{origin_name}出发，{self._transition_route_text(route)}"
                else:
                    item["detail"] = f"补充{label}，就近安排。"
                its.append(item)

        # 全天时间重排：转场用真实路线耗时，并把相邻块之间的空窗压到 ~10 分钟，
        # 消除"转场 11:00-11:10 → 餐饮 12:00"这类到达后空等 50 分钟的问题。
        def _m_to_min(s):
            try:
                h, m = str(s).split(":")
                return int(h) * 60 + int(m)
            except (ValueError, AttributeError, TypeError):
                return None

        def _m_fmt(m):
            return f"{m // 60:02d}:{m % 60:02d}"

        _REF_BLOCK_BUFFER = 10  # 相邻两块之间的最短间隔（分钟）

        for dy in daily_plan:
            its = dy.get("items") or []
            if not its:
                continue
            # 1) 计算每个块的真实时长：交通/返程用真实路线耗时（已向上取 5 的整数），其余沿用原有起止区间
            spans = []
            for it in its:
                s, e = _m_to_min(it.get("start_time")), _m_to_min(it.get("end_time"))
                if it.get("item_type") in ("transport", "return"):
                    dur = (it.get("transport") or {}).get("duration_minutes")
                    if dur and int(dur) > 0:
                        spans.append(max(int(dur), 5))
                    else:
                        spans.append((e - s) if s is not None and e is not None and e > s else 10)
                elif s is not None and e is not None and e > s:
                    spans.append(e - s)
                else:
                    spans.append(60)
            # 2) 从头向后紧凑排布：保留首个块的原起始，之后每块 = 上一块结束 + 10 分钟缓冲，
            #    交通到达与下一活动之间自然只隔 10 分钟，消除大空窗且保证不重叠。
            cursor = _m_to_min(its[0].get("start_time")) or 8 * 60
            for i, it in enumerate(its):
                it["start_time"] = _m_fmt(cursor)
                cursor += spans[i]
                it["end_time"] = _m_fmt(cursor)
                cursor += _REF_BLOCK_BUFFER

        stay_recommendation = []
        if lodging_anchor.anchor_mode == "use_selected_lodging" and state.selected_lodging:
            stay_recommendation.append(
                {
                    "name": state.selected_lodging.name,
                    "area": state.selected_lodging.area,
                    "selected": True,
                }
            )
        else:
            for hotel in lodging_candidates[:3]:
                if self._is_displayable_lodging(hotel.name):
                    stay_recommendation.append({"name": hotel.name, "area": hotel.area})
        for entry in stay_recommendation:
            lng, lat = self._geocode_point(entry["name"], destination)
            if lng is not None and lat is not None:
                entry["lng"] = lng
                entry["lat"] = lat

        weather_notes = self._build_weather_notes(weather_days)
        transport_plan = self._build_transport_summary(draft, transport_results=state.transport_results)

        # 备选清单剔除已在行程中使用的景点，避免“备选”与“已选”重叠
        used_titles = {
            item["title"] for day in daily_plan for item in day["items"] if item["item_type"] == "attraction"
        }
        alternatives = [spot for spot in (skeleton.rejected_spots_global or []) if spot not in used_titles][:5]

        return TripPlan(
            destination=destination,
            summary=draft.summary if draft.summary else skeleton.summary,
            route_intent_summary=draft.route_intent_summary if draft.route_intent_summary else skeleton.overall_rationale,
            daily_plan=daily_plan,
            stay_recommendation=stay_recommendation,
            transport_plan=transport_plan,
            weather_notes=weather_notes,
            alternatives=alternatives,
            reflection=None,
        )

    def _fetch_transport_route(self, destination: str, from_coord: tuple | None, to_coord: tuple | None) -> dict | None:
        """按坐标调高德规划真实路线（步行/打车/公交），取耗时最短方案，返回模式/时长/距离/费用/轨迹。

        任一次级 API 失败都独立降级：返回步行、打车、公交三者中可用且最短的方案；全部失败返回 None。
        坐标已在上层解析，避免 transport tool 二次 POI 检索，减少配额消耗。"""
        if not from_coord or not to_coord:
            return None
        origin = (from_coord[0], from_coord[1])
        dest = (to_coord[0], to_coord[1])

        def safe(fn):
            try:
                return fn()
            except Exception:
                return None

        # 三种出行方式彼此独立，串行请求时每个转场要等 3 次往返（~3×），
        # 改为并行一次性拿齐，单转场耗时收敛到三次中最慢的一次。
        try:
            with ThreadPoolExecutor(max_workers=3) as _mode_ex:
                ft_taxi = _mode_ex.submit(safe, lambda: amap_client.plan_driving(origin=origin, destination=dest))
                ft_transit = _mode_ex.submit(safe, lambda: amap_client.plan_transit(origin=origin, destination=dest, city=destination))
                ft_walk = _mode_ex.submit(safe, lambda: amap_client.plan_walking(origin=origin, destination=dest))
                taxi = ft_taxi.result()
                transit = ft_transit.result()
                walk = ft_walk.result()
        except Exception:
            return None

        candidates = []
        if taxi and taxi.get("duration_seconds") is not None:
            candidates.append((round(taxi["duration_seconds"] / 60), "打车", taxi.get("distance_meters"), taxi.get("cost"), taxi.get("polyline_points")))
        if transit and transit.get("duration_seconds") is not None:
            candidates.append((round(transit["duration_seconds"] / 60), "地铁/公交", transit.get("distance_meters"), transit.get("price"), transit.get("polyline_points")))
        if walk and walk.get("duration_seconds") is not None:
            candidates.append((round(walk["duration_seconds"] / 60), "步行", walk.get("distance_meters"), None, walk.get("polyline_points")))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        duration, mode, distance_meters, cost, path = candidates[0]
        # 预估时长向上取 5 的整数（6→10、13→15），便于用户预期，最低 5 分钟
        duration = max(((duration + 4) // 5) * 5, 5)
        return {
            "mode": mode,
            "duration_minutes": duration,
            "distance_km": round(distance_meters / 1000, 1) if distance_meters else None,
            "cost": cost,
            "path": path or None,
            # 公交/地铁命中时给出“像导航一样”的分段转场描述
            "nav": self._transit_nav_text(transit) if (mode == "地铁/公交" and transit) else None,
        }

    @staticmethod
    def _transition_route_text(route: dict, from_name: str | None = None, to_name: str | None = None) -> str:
        """把一条真实路线格式化为可读的换乘建议文本；公交/地铁有分段导航时优先展示导航步骤。"""
        if route.get("nav"):
            base = route["nav"]
            if route.get("duration_minutes") or route.get("cost"):
                tail = "，".join(
                    part
                    for part in [
                        f"约{route['duration_minutes']}分钟" if route.get("duration_minutes") else "",
                        f"约¥{route['cost']}" if route.get("cost") else "",
                    ]
                    if part
                )
                if tail:
                    base = f"{base}（{tail}）"
            if from_name or to_name:
                return f"{from_name or '起点'} → {to_name or '终点'}：{base}"
            return base
        parts = [f"建议{route.get('mode', '公共交通')}"]
        if route.get("duration_minutes"):
            parts.append(f"约{route['duration_minutes']}分钟")
        if route.get("distance_km"):
            parts.append(f"{route['distance_km']}公里")
        if route.get("cost"):
            parts.append(f"约¥{route['cost']}")
        text = "，".join(parts) + "。"
        if from_name or to_name:
            return f"{from_name or '起点'} → {to_name or '终点'}，{text}" if from_name else text
        return text

    @staticmethod
    def _transit_nav_text(transit: dict | None) -> str | None:
        """从高德公交响应提取最高优方案的分段导航文本，如：步行300米 → 乘地铁5号线(珠江新城→动物园)。

        仅用于给用户“像导航一样”的换乘感知；解析失败或数据缺失时返回 None，不影响主流程。"""
        if not isinstance(transit, dict):
            return None
        try:
            transits = (transit.get("transits") or [])
            if not transits:
                return None
            segments = (transits[0] or {}).get("segments") or []
        except Exception:
            return None
        parts: list[str] = []
        try:
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                walking = segment.get("walking")
                if isinstance(walking, dict) and walking.get("distance"):
                    dist = float(walking["distance"])
                    parts.append(f"步行{round(dist)}米")
                bus = segment.get("bus")
                if not isinstance(bus, dict):
                    continue
                buslines = bus.get("buslines") or []
                if not buslines or not isinstance(buslines[0], dict):
                    continue
                bl = buslines[0]
                name = str(bl.get("name") or "")
                dep = (bl.get("departure_stop") or {}).get("name") if isinstance(bl.get("departure_stop"), dict) else None
                arr = (bl.get("arrival_stop") or {}).get("name") if isinstance(bl.get("arrival_stop"), dict) else None
                if name:
                    leg = f"乘{name}"
                    if dep and arr:
                        leg += f"({dep}→{arr})"
                    parts.append(leg)
        except Exception:
            return None
        if not parts:
            return None
        return " → ".join(parts)

    @staticmethod
    def _coord_dist(p: tuple | list | None, q: tuple | list | None) -> float:
        """近似平面距离平方，仅用于同城候选排序（无需高精度）。"""
        if not p or not q:
            return float("inf")
        return (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2

    @staticmethod
    def _downsample_path(path, max_points: int = 48):
        """对高德返回的真实轨迹抽稀：尽量保留走向，同时避免动辄数百上千个坐标点塞进输出。"""
        if not path:
            return None
        n = len(path)
        if n <= max_points:
            return path
        step = (n - 1) / (max_points - 1)
        return [path[round(i * step)] for i in range(max_points)]

    def _clean_block_times(self, blocks: list[ItineraryTimeBlockSchema]) -> list[ItineraryTimeBlockSchema]:
        """清洗时间块：修正零时长（start==end）与时间倒挂（end<start），并避免与上一块重叠。

        这是对 LLM 成稿的防御性修正，保证时间线单调推进，供前端/地图展示使用。"""
        cleaned: list[ItineraryTimeBlockSchema] = []
        prev_end: str | None = None
        for block in blocks:
            start = block.start_time
            end = block.end_time
            if start and end and end <= start:
                end = self._bump_time(start)
            if prev_end and start and start < prev_end:
                start = prev_end
                if end and end <= start:
                    end = self._bump_time(start)
            if not end:
                end = self._bump_time(start or "10:00")
            cleaned.append(block.model_copy(update={"start_time": start, "end_time": end}))
            prev_end = end
        return cleaned

    @staticmethod
    def _bump_time(hhmm: str) -> str:
        """'HH:MM' 向后推 1 小时（23:00 封顶），用于修补零时长/倒挂的结束时间。"""
        try:
            hour, minute = hhmm.split(":")
            minutes = int(hour) * 60 + int(minute) + 60
            minutes = min(minutes, 23 * 60)
            return f"{minutes // 60:02d}:{minutes % 60:02d}"
        except (ValueError, AttributeError):
            return "10:00"

    def _build_poi_index(self, state: PlanningContext) -> dict[str, dict]:
        """把景点/餐饮候选按名称建索引（含 poi_id/lng/lat/address），供最终行程按标题回填真实坐标。

        优先用高德 POI 原文坐标（更准、不耗 geocode 配额）；餐饮优先，同名时覆盖景点。"""
        index: dict[str, dict] = {}
        candidates = []
        if state.attraction_result:
            candidates.extend(state.attraction_result.candidates or [])
        if state.meal_result:
            candidates.extend(state.meal_result.candidates or [])
        for cand in candidates:
            normalized = self._normalize_poi_name(cand.name)
            if not normalized:
                continue
            index[normalized] = {
                "poi_id": cand.poi_id,
                "lng": cand.lng,
                "lat": cand.lat,
                "address": cand.address,
            }
        return index

    @staticmethod
    def _normalize_poi_name(value: str) -> str:
        """名称归一化：去掉餐饮语义前缀（“午餐：全聚德”→“全聚德”）并去除空白与常见标点，
        供行程块标题 ↔ 候选池按名匹配。"""
        text = str(value).strip()
        for prefix in ("早餐", "午餐", "晚餐", "宵夜", "夜宵", "前往", "返回", "弹性", "目的地", "附近美食"):
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip("：: ")
        return "".join(
            ch
            for ch in text.lower()
            if not ch.isspace() and ch not in {"-", "_", "（", "）", "(", ")", "·", "/", "，", ",", "：", ":"}
        )

    def _geocode_point(self, name: str, city: str) -> tuple[float | None, float | None]:
        """对景点/住宿名做高德地理编码，返回 (lng, lat)；失败/未配置返回 (None, None)。带进程内缓存。"""
        key = (name, city)
        if key in self._geocode_cache:
            return self._geocode_cache[key]
        coord: tuple[float | None, float | None] = (None, None)
        try:
            if amap_client.is_enabled():
                result = amap_client.geocode(address=name, city=city)
                if result and result.get("lng") is not None and result.get("lat") is not None:
                    coord = (result["lng"], result["lat"])
        except Exception:
            coord = (None, None)
        self._geocode_cache[key] = coord
        return coord

    def _build_weather_notes(self, weather_days: list) -> list[str]:
        """逐日天气备注：日期 + 天气 + 温度 + 风 + 湿度 + 降水；无天气数据的天不输出占位。"""
        notes: list[str] = []
        for item in weather_days:
            if not (item.weather_day or item.temperature_range):
                continue
            parts = [
                str(item.date or ""),
                item.weather_day,
                item.temperature_range,
                item.wind,
                f"湿度{item.humidity}" if item.humidity else None,
                f"降水{item.precip}" if item.precip else None,
            ]
            notes.append(" ".join(p for p in parts if p))
        return notes

    def _build_transport_summary(self, draft: ItineraryDraftSchema, transport_results: list | None = None) -> list[dict]:
        """构建 transport_plan：优先注入 transport tool 真实证据（模式/时长/距离/费用），
        无精确证据时按“同区域/跨区”给出合理的转场建议，不再输出纯占位文案。"""
        evidence = {
            (result.from_name, result.to_name): result
            for result in transport_results or []
            if result.from_name and result.to_name
        }
        summary = []
        for day in draft.day_plans:
            transitions = []
            blocks = day.time_blocks
            for idx in range(len(blocks) - 1):
                current_block = blocks[idx]
                next_block = blocks[idx + 1]
                if current_block.item_type in {"return", "transport"}:
                    continue
                transition = {"from": current_block.title, "to": next_block.title}
                route = evidence.get((current_block.title, next_block.title))
                mode_summary = pick_transport_mode(route) if route is not None else None
                if mode_summary:
                    transition.update(mode_summary)
                    transition["advice"] = self._format_transport_advice(mode_summary)
                else:
                    transition["advice"] = self._fallback_transport_advice(current_block, next_block)
                transitions.append(transition)
            if transitions:
                summary.append({"day_index": day.day_index, "transitions": transitions})
        return summary

    @staticmethod
    def _format_transport_advice(mode_summary: dict) -> str:
        parts = [f"建议{mode_summary.get('recommended_mode', '公共交通')}"]
        if mode_summary.get("duration_minutes"):
            parts.append(f"约{mode_summary['duration_minutes']}分钟")
        if mode_summary.get("distance_km"):
            parts.append(f"{mode_summary['distance_km']}公里")
        if mode_summary.get("cost"):
            parts.append(f"约¥{mode_summary['cost']}")
        return "，".join(parts) + "。"

    @staticmethod
    def _fallback_transport_advice(current_block, next_block) -> str:
        area_from = current_block.area
        area_to = next_block.area
        if area_from and area_to and area_from == area_to:
            return f"同属{area_from}，建议步行或打车前往；如需精确路线可查看地图实时导航。"
        if area_from and area_to:
            return f"跨区转场（{area_from} → {area_to}），建议地铁或打车，预留换乘时间。"
        return "建议查看地图实时路线，选择地铁或打车。"

    def _execute_tool_step(
        self,
        state: PlanningContext,
        tool_name: str,
        progress: Callable[[str, dict], None] | None = None,
    ) -> dict:
        self._emit_progress(progress, _TOOL_STAGE_LABELS.get(tool_name, f"正在调用 {tool_name} 工具…"))
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
            state.selected_lodging = self._pick_selected_lodging(state.lodging_result.candidates if state.lodging_result else None)
            return {
                "tool": "lodging",
                "candidate_count": len(state.lodging_result.candidates) if state.lodging_result else 0,
                "selected_lodging": state.selected_lodging.name if state.selected_lodging else None,
            }
        if tool_name == "meal":
            state.meal_result = self._run_meals(request, state)
            return {
                "tool": "meal",
                "candidate_count": len(state.meal_result.candidates) if state.meal_result else 0,
            }
        if tool_name == "transport":
            return {
                "tool": "transport",
                "route_count": len(state.transport_results),
                "status": "deferred_until_skeleton",
            }
        return {"tool": tool_name, "status": "skipped"}

    def _run_weather(self, request: PlanningRequest, start_date: str | None = None, end_date: str | None = None):
        if not start_date or not end_date:
            start_date, end_date = self._resolve_dates(request)
        return weather_tool.run(WeatherInput(city=request.destination, start_time=start_date, end_time=end_date))

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
        spots = [item.name for item in candidates][:6]
        preferences = list(request.preferences) + ["优先标准酒店", "适合作为全程锚点"]
        return lodging_tool.run(
            LodgingInput(
                destination=request.destination,
                preferences=preferences,
                avoid_keywords=list(request.avoid_spots) + ["招待所"],
                spots=spots,
            )
        )

    def _run_meals(self, request: PlanningRequest, state: PlanningContext):
        candidates = state.attraction_result.candidates if state.attraction_result else []
        spots = [item.name for item in candidates][:10]
        preferences = _extract_meal_preferences(request)
        return meal_tool.run(
            MealInput(
                destination=request.destination,
                preferences=preferences,
                spots=spots,
                top_n=16,
            )
        )

    def _build_day_meal_pools(self, request: PlanningRequest, state: PlanningContext, skeleton: PlanningSkeleton):
        """骨架定下每天景点分布后，按天分别查询就近餐饮候选（可多次调用餐饮工具）。

        不依赖单一全局池：每天都以当天实际景点为锚点单独发起一次餐饮查询，
        这样不同片区的景点都有就近餐馆可选，避免「餐馆集中在某一片/离酒店近」。
        """
        pools: dict[int, list[dict]] = {}
        preferences = _extract_meal_preferences(request)
        avoid = list(request.avoid_spots) + ["招待所"]
        day_spots: list[tuple[int, list[str]]] = []
        for day in skeleton.day_skeletons:
            spots = list(dict.fromkeys((day.selected_spots or []) + (day.optional_spots or [])))[:8]
            if spots:
                day_spots.append((day.day_index, spots))

        def _query_one(pair: tuple[int, list[str]]) -> tuple[int, list[dict]] | None:
            day_index, spots = pair
            try:
                res = meal_tool.run(
                    MealInput(
                        destination=request.destination,
                        preferences=preferences,
                        avoid_keywords=avoid,
                        spots=spots,
                        top_n=8,
                    )
                )
            except Exception:
                return None
            return day_index, [
                {"name": c.name, "area": c.area, "lng": c.lng, "lat": c.lat}
                for c in (res.candidates if res else [])
                if c.lng is not None
            ]

        with ThreadPoolExecutor(max_workers=min(len(day_spots), 4)) as _ex:
            for future in as_completed([_ex.submit(_query_one, pair) for pair in day_spots]):
                got = future.result()
                if got:
                    pools[got[0]] = got[1]
        state.day_meal_pool = pools
        return pools

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

    def _should_collect_lodging(self, state: PlanningContext) -> bool:
        return state.request.days >= 2

    def _normalize_block_title(self, title: str, item_type: str) -> str:
        title = title.strip()
        if item_type == "transport" and not title.startswith(("前往", "步行至", "乘", "打车至", "地铁至", "返回", "从")):
            return f"前往{title}"
        if item_type == "meal" and not title.startswith(("午餐", "晚餐", "早餐")):
            return f"午餐：{title}"
        if item_type == "return" and not title.startswith("返回"):
            return f"返回{title}"
        if item_type == "flex" and not title.startswith("弹性"):
            return f"弹性：{title}"
        return title

    def _canonical_spot_name(self, state: PlanningContext, raw: str | None) -> str | None:
        """把 LLM 输出的景点名归一化为候选池规范名；映射不到返回 None。

        匹配策略逐级放宽：
        1. 与候选名完全相等；
        2. 忽略空白后相等；
        3. LLM 名是候选名子串（LLM 写简称/别名）→ 取包含它的最短候选；
        4. 候选名是 LLM 名子串（LLM 写全称/带修饰）→ 取被包含的最长候选。
        """
        if not raw:
            return None
        name = raw.strip()
        candidates = list(state.attraction_result.candidates or []) if state.attraction_result else []
        if not candidates:
            return name or None
        compact = "".join(name.split())
        for item in candidates:
            if item.name == name:
                return item.name
        for item in candidates:
            if item.name and "".join(item.name.split()) == compact:
                return item.name
        contained = [item.name for item in candidates if len(item.name) >= 2 and name in item.name]
        if contained:
            return min(contained, key=len)
        subsumed = [item.name for item in candidates if len(item.name) >= 2 and item.name in name]
        if subsumed:
            return max(subsumed, key=len)
        return None

    def _canonicalize_skeleton_spots(self, state: PlanningContext, skeleton: PlanningSkeleton) -> PlanningSkeleton:
        """把 skeleton 里所有景点名统一为候选池规范名。

        LLM 输出的别名/缩写/扩展名都能映射回候选池，映射不到的（LLM 硬造的新点）
        直接丢弃，避免整轮规划因个别名字失配而失败。
        """
        skeleton.selected_spots_global = [
            name for name in (self._canonical_spot_name(state, spot) for spot in skeleton.selected_spots_global) if name
        ]
        for day in skeleton.day_skeletons:
            day.selected_spots = [
                name for name in (self._canonical_spot_name(state, spot) for spot in day.selected_spots) if name
            ]
            # 主点被丢空时，优先把可选点提升为主点，保住当天主线
            if not day.selected_spots:
                promoted = [name for name in (self._canonical_spot_name(state, spot) for spot in day.optional_spots) if name]
                if promoted:
                    day.selected_spots = promoted[:2]
                    day.optional_spots = []
            else:
                day.optional_spots = [
                    name
                    for name in (self._canonical_spot_name(state, spot) for spot in day.optional_spots)
                    if name and name not in day.selected_spots
                ]
            day.rejected_spots = [
                name
                for name in (self._canonical_spot_name(state, spot) for spot in day.rejected_spots)
                if name and name not in day.selected_spots and name not in day.optional_spots
            ]
            for target in day.transport_check_targets:
                mapped_from = self._canonical_spot_name(state, target.from_label)
                mapped_to = self._canonical_spot_name(state, target.to_label)
                if mapped_from:
                    target.from_label = mapped_from
                if mapped_to:
                    target.to_label = mapped_to
        return skeleton

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

    def _build_fallback_cluster_plan(self, state: PlanningContext) -> ClusterPlanning | None:
        """LLM 主题规划失败时的规则兜底：把候选景点按区域聚成主题簇。

        仅为下一步骨架提供过渡素材，不生成任何最终行程内容；最终输出仍由 LLM 渲染
        （与 _build_fallback_skeleton_from_clusters 的按区分组逻辑保持一致）。
        """
        candidates = list(state.attraction_result.candidates or []) if state.attraction_result else []
        if not candidates:
            return None
        # 按区域分组，每簇取多个候选，避免兜底骨架每天景点过于单薄
        by_area: dict[str, list] = {}
        for item in candidates:
            by_area.setdefault(item.area or "未分区", []).append(item)
        clusters: list[CandidateCluster] = []
        for area, area_items in by_area.items():
            primary = area_items[0]
            selected = [item.name for item in area_items[:3]]
            optional = [item.name for item in area_items[3:]]
            clusters.append(
                CandidateCluster(
                    cluster_id=f"fallback_{len(clusters) + 1}",
                    label=area,
                    selected_spots=selected,
                    optional_spots=optional,
                    rejected_spots=[],
                    why_it_works=f"以{area}内的高价值候选先形成基本主题簇。",
                    weather_fit=None,
                    effort_level="balanced",
                    night_closure_style="晚餐后在主簇附近完成正式晚间收尾。",
                    must_stay_together=False,
                    is_remote_day_candidate=bool(
                        getattr(primary, "estimated_visit_duration_hours", 0)
                        and getattr(primary, "estimated_visit_duration_hours", 0) >= 4.5
                    ),
                )
            )
        if not clusters:
            return None
        return ClusterPlanning(
            destination=state.request.destination,
            summary=f"{state.request.destination} 初步主题规划。",
            overall_rationale="按候选景点所在区域分组形成的过渡规划。",
            clusters=clusters,
            rejected_spots_global=[],
            needs_attraction_refresh=False,
            attraction_refresh_reason=None,
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

    @staticmethod
    def _clean_route_verb(title: str | None) -> str:
        """去掉转场/返程块的动词前缀，只留下可用于 geocode 的地名。"""
        text = str(title or "").strip()
        for verb in ("从", "前往", "返回", "回到", "出发去", "转场到"):
            text = text.replace(verb, "", 1)
        text = re.sub(r"^(从|前往|返回|回到|出发去|转场到)+", "", text)
        return text.strip()

    @staticmethod
    def _is_generic_meal_title(title: str | None) -> bool:
        """判断餐饮块标题是否为无具体店名的泛称。"""
        t = str(title or "").strip()
        if any(m in t for m in ("休整", "休息", "自由安排", "自选", "就近用餐", "顺路", "简餐", "自理", "短暂")):
            return True
        stripped = t
        for prefix in ("早餐", "午餐", "晚餐", "宵夜", "夜宵"):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix):].lstrip("：: ")
                break
        return stripped.strip() in {"", "用餐", "吃饭", "美食", "小吃", "简单地吃"}

    @staticmethod
    def _is_leaked_id_title(title) -> bool:
        """检测标题是否为高德 poi_id 等纯字母数字 ID（真实景点/餐馆名必含中文）"""
        return bool(re.fullmatch(r"[A-Za-z0-9]{8,}", str(title or "").strip()))

    _MEAL_BOILERPLATE_MARKERS = (
        r"建议在[^。，]*选择本地口碑餐馆[。，]?",
        r"建议在[^。，]*安排[早晚]餐[。，]?",
        r"建议在[^。，]*就近[就餐用餐][。，]?",
        r"优先选择可步行到交通点的餐厅[。]?",
        r"避免连续高强度步行[。]?",
        r"就近用餐[。]?",
        r"当地[^。]*特色[。]?",
    )

    # 转场/返程块里 LLM 生成的泛称兜底文案（无真实坐标/路线时才会保留），渲染失败时剥掉
    _TRANSITION_BOILERPLATE_MARKERS = (
        r"建议(当天|当日)?最后一段优先(打车|地铁|公交)返程[。，]?",
        r"避免夜间多次换乘[。]?",
        r"控制出发时间并避开拥堵[。，]?",
        r"建议优先(地铁|打车|公交)[，。]?",
        r"注意避开(晚|早)高峰[。，]?",
        r"防止(堵车|拥堵)[。，]?",
    )

    @staticmethod
    def _strip_markers(text, markers) -> str:
        if not text:
            return ""
        cleaned = str(text)
        for pat in markers:
            cleaned = re.sub(pat, "", cleaned)
        return re.sub(r"\s*[。，]\s*$", "", cleaned).strip()

    def _strip_meal_boilerplate(self, text) -> str:
        """去掉餐块 detail 里的模板套话，保留真实衔接说明。"""
        if not text:
            return ""
        cleaned = str(text)
        for pat in self._MEAL_BOILERPLATE_MARKERS:
            cleaned = re.sub(pat, "", cleaned)
        return re.sub(r"\s*[。，]\s*$", "", cleaned).strip()

    def _shorten_attraction_detail(self, detail: str | None, max_len: int = 120) -> str | None:
        """景点详情精简：只保留最前 2 个要点并截断，避免一整段堆叠。"""
        if not detail:
            return detail
        clauses = [c.strip() for c in str(detail).split("；") if c.strip()]
        kept = clauses[:2] or [str(detail).strip()]
        out = "，".join(kept)
        if len(out) > max_len:
            out = out[:max_len].rstrip("，;、") + "…"
        return out

    @staticmethod
    def _pick_selected_lodging(candidates):
        """在评分靠前的住宿候选中按权重随机选一家，让同类行程输出有差异而不是永远同一家。

        仍以前几名（默认前 4）为主，权重随名次递减：质量优先、同时允许多次生成选到不同酒店。
        """
        if not candidates:
            return None
        pool = list(candidates)[:4]
        if len(pool) <= 1:
            return pool[0]
        weights = [max(0.1, 1.0 - 0.25 * i) for i in range(len(pool))]
        return random.choices(pool, weights=weights, k=1)[0]

    @staticmethod
    def _pick_meal_candidate(
        meal_pool: list[dict],
        used: set[str],
        prev_coord: tuple | list | None,
        next_coord: tuple | list | None,
        block_area: str | None,
        day_area: str | None,
    ) -> dict | None:
        """为某顿餐选真实餐馆：兼顾上一景点与下一景点，选离转场路径两端都不太远的未使用候选。

        优先紧邻上一景点（刚逛完就该到），若上一无坐标则看下一；区域匹配作为次级加分。
        """
        if not meal_pool:
            return None
        unused = [c for c in meal_pool if c["name"] not in used]
        if not unused:
            return None
        area_to = (block_area or "").strip()
        day_area = (day_area or "").strip()

        def key(cand: dict):
            coord = (cand.get("lng"), cand.get("lat"))
            d_prev = PlanningAgent._coord_dist(prev_coord, coord)
            d_next = PlanningAgent._coord_dist(next_coord, coord)
            # 两端都可达时取较近的那端，避免“紧贴上一但离下一极远”；仅其一时用可得端
            if d_prev != float("inf") and d_next != float("inf"):
                dist = max(d_prev, d_next) * 0.6 + min(d_prev, d_next) * 0.4
            elif d_prev != float("inf"):
                dist = d_prev
            elif d_next != float("inf"):
                dist = d_next
            else:
                dist = float("inf")
            area_bonus = 0
            area = (cand.get("area") or "").strip()
            if area_to and area == area_to:
                area_bonus += 2
            if day_area and area == day_area:
                area_bonus += 1
            return (dist, -area_bonus)

        best = min(unused, key=key)
        used.add(best["name"])
        return best

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
        # 去掉"天气：""预约："这类只有前缀、冒号后无实质内容的占位备注
        normalized = [note for note in normalized if note.split("：", 1)[-1].strip()]
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
