from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.schema.planning import PlanningRequest, TripPlan
from app.domain.common.session_context import SessionContext
from app.domain.common.user import UserContext
from app.infrastructure.llm.schemas import ItineraryDayPlan, ItineraryDraftSchema


class RevisionIntent(BaseModel):
    user_message: str
    change_scope: Literal["block_level", "day_level", "global"]
    revision_goal: str
    affected_days: list[int] = Field(default_factory=list)
    affected_block_ids: list[str] = Field(default_factory=list)
    locked_days: list[int] = Field(default_factory=list)
    locked_spots: list[str] = Field(default_factory=list)
    locked_blocks: list[str] = Field(default_factory=list)
    removed_spots: list[str] = Field(default_factory=list)
    added_spots: list[str] = Field(default_factory=list)
    preserve_unchanged_days: bool = True
    pace_change: Literal["slower", "faster", "unchanged"] | None = None
    style_shift: list[str] = Field(default_factory=list)
    budget_change: str | None = None
    lodging_change: bool = False
    weather_replan: bool = False
    transport_replan: bool = False
    expected_outcomes: list[str] = Field(default_factory=list)
    forbidden_outcomes: list[str] = Field(default_factory=list)


class ReviseExecutionPolicy(BaseModel):
    response_mode: Literal["full_plan", "fast_plan", "revise_plan"] = "revise_plan"
    include_summary: bool = True
    include_daily_plan: bool = True
    include_stay_recommendation: bool = True
    include_transport_plan: bool = True
    include_weather_notes: bool = True
    include_alternatives: bool = True
    preserve_existing_structure: bool = True
    preserve_unchanged_days: bool = True
    allow_tool_refresh: bool = True
    prefer_local_patch: bool = True
    allow_global_rewrite: bool = False
    allow_day_rebuild: bool = True
    allow_block_patch: bool = True
    max_repair_attempts: int = 1


class RevisionImpactAnalysis(BaseModel):
    scope: Literal["block_level", "day_level", "global"]
    affected_days: list[int] = Field(default_factory=list)
    affected_block_ids: list[str] = Field(default_factory=list)
    reused_days: list[int] = Field(default_factory=list)
    locked_days: list[int] = Field(default_factory=list)
    requires_tool_refresh: bool = False
    required_tools: list[Literal["weather", "attraction", "lodging", "transport"]] = Field(default_factory=list)
    should_rebuild_skeleton: bool = False
    should_rerender_full_draft: bool = False
    reason: str | None = None


class ReviseDebugTrace(BaseModel):
    step: str
    status: str | None = None
    message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    elapsed_seconds: float | None = None


class ReviseArtifacts(BaseModel):
    draft: ItineraryDraftSchema
    plan: TripPlan


class BlockLevelReviseResultSchema(BaseModel):
    affected_days: list[int] = Field(default_factory=list)
    day_plans: list[ItineraryDayPlan] = Field(default_factory=list)
    changed_blocks_summary: list[str] = Field(default_factory=list)
    revised_summary_fragment: str | None = None


class DayLevelReviseResultSchema(BaseModel):
    affected_days: list[int] = Field(default_factory=list)
    day_plans: list[ItineraryDayPlan] = Field(default_factory=list)
    revised_summary_fragment: str | None = None


class ReviseAgentInput(BaseModel):
    request: PlanningRequest
    user_context: UserContext
    session_context: SessionContext
    execution_policy: ReviseExecutionPolicy = Field(default_factory=ReviseExecutionPolicy)
    current_plan: TripPlan
    current_draft: ItineraryDraftSchema | None = None
    revision_intent: RevisionIntent
    bootstrap_intent: dict[str, Any] = Field(default_factory=dict)
    prior_final_state: dict[str, Any] = Field(default_factory=dict)
    prior_planning_trace: list[dict[str, Any]] = Field(default_factory=list)
    trip_history: list[dict[str, Any]] = Field(default_factory=list)  # 历史行程摘要（按目的地去重）


class ReviseAgentOutput(BaseModel):
    artifacts: ReviseArtifacts
    revision_intent: RevisionIntent
    impact_analysis: RevisionImpactAnalysis
    revision_trace: list[ReviseDebugTrace] = Field(default_factory=list)
    revision_summary: dict[str, Any] = Field(default_factory=dict)
    status: Literal["completed", "needs_follow_up", "failed"] = "completed"
    summary: str | None = None


__all__ = [
    "BlockLevelReviseResultSchema",
    "DayLevelReviseResultSchema",
    "ReviseAgentInput",
    "ReviseAgentOutput",
    "ReviseArtifacts",
    "ReviseDebugTrace",
    "ReviseExecutionPolicy",
    "RevisionImpactAnalysis",
    "RevisionIntent",
]
