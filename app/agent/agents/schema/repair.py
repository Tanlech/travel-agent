from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agent.agents.schema.planning import PlanningRequest, TripPlan
from app.agent.agents.schema.reflection import ReflectionResult
from app.agent.agents.schema.revise import RevisionIntent
from app.agent.domain.common.itinerary import ItineraryDraftSchema, ItinerarySpotRefSchema


class RepairDayPlanSchema(BaseModel):
    """修复后的单日安排（repair 产物，定义在 agents 层）"""

    day_index: int
    primary_area: str | None = None
    spots: list[ItinerarySpotRefSchema] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    rationale: str | None = None


class RepairProposalSchema(BaseModel):
    destination: str
    summary: str
    modified_days: list[int] = Field(default_factory=list)
    day_plans: list[RepairDayPlanSchema] = Field(default_factory=list)


class RepairInstruction(BaseModel):
    repair_scope: Literal["block_level", "day_level", "global"]
    target_days: list[int] = Field(default_factory=list)
    target_block_ids: list[str] = Field(default_factory=list)
    locked_days: list[int] = Field(default_factory=list)
    locked_spots: list[str] = Field(default_factory=list)
    must_fix_issue_types: list[str] = Field(default_factory=list)
    keep_unchanged_elsewhere: bool = True
    instruction_summary: str


class RepairUserContext(BaseModel):
    preferred_styles: list[str] = Field(default_factory=list)
    disliked_styles: list[str] = Field(default_factory=list)
    accept_theme_park: bool | None = None
    accept_nightlife: bool | None = None
    pace_preference: Literal["relaxed", "dense"] | None = None
    family_friendly: bool | None = None
    senior_friendly: bool | None = None


class RepairSessionContext(BaseModel):
    session_id: str | None = None
    confirmed_fields: list[str] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
    conversation_stage: str = "repair"
    last_destination: str | None = None
    revision_count: int = 0


class RepairExecutionPolicy(BaseModel):
    response_mode: Literal["full_plan", "fast_plan", "revise_plan"] = "full_plan"
    prefer_local_patch: bool = True
    max_repair_attempts: int = 1


class RepairDebugTrace(BaseModel):
    step: str
    status: str | None = None
    message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    elapsed_seconds: float | None = None


class RepairArtifacts(BaseModel):
    draft: ItineraryDraftSchema
    plan: TripPlan


class RepairInput(BaseModel):
    request: PlanningRequest
    user_context: RepairUserContext
    session_context: RepairSessionContext
    execution_policy: RepairExecutionPolicy = Field(default_factory=RepairExecutionPolicy)
    source: Literal["plan", "revise"]
    current_draft: ItineraryDraftSchema
    current_plan: TripPlan | None = None
    reflection_result: ReflectionResult
    revision_intent: RevisionIntent | None = None
    repair_instruction: RepairInstruction | None = None
    final_state: dict[str, Any] = Field(default_factory=dict)
    trace: list[dict[str, Any]] = Field(default_factory=list)


class RepairResult(BaseModel):
    destination: str
    summary: str
    modified_days: list[int] = Field(default_factory=list)
    day_plans: list[RepairDayPlanSchema] = Field(default_factory=list)


class RepairAgentOutput(BaseModel):
    artifacts: RepairArtifacts
    repaired_issues: list[str] = Field(default_factory=list)
    unresolved_issues: list[str] = Field(default_factory=list)
    repair_trace: list[RepairDebugTrace] = Field(default_factory=list)
    status: Literal["completed", "partial", "failed"] = "completed"
    summary: str | None = None


__all__ = [
    "RepairAgentOutput",
    "RepairArtifacts",
    "RepairDayPlanSchema",
    "RepairDebugTrace",
    "RepairExecutionPolicy",
    "RepairInput",
    "RepairInstruction",
    "RepairProposalSchema",
    "RepairResult",
    "RepairSessionContext",
    "RepairUserContext",
]
