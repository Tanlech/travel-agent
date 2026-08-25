from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.agent.domain.common.planning import PlanningRequest, TripPlan  # re-export（定义见 domain.common.planning）


ToolName = Literal["weather", "attraction", "lodging", "transport", "none"]
PlannerStatus = Literal["need_tool", "enough_to_plan"]
PacingMode = Literal["relaxed", "balanced", "dense"]
AnchorMode = Literal["use_selected_lodging", "use_city_center_placeholder", "stay_unconfirmed"]
ClusterEffort = Literal["light", "balanced", "dense"]
LodgingAnchorStatus = Literal["validated", "acceptable", "needs_refresh", "invalid"]


class DialogueDecision(BaseModel):
    status: str
    missing_fields: list[str] = Field(default_factory=list)
    follow_up_question: str | None = None


class PlanInput(BaseModel):
    request: PlanningRequest
    user_id: str | None = None
    session_id: str | None = None


class PlanningNextAction(BaseModel):
    status: PlannerStatus
    next_tool: ToolName
    reason: str
    missing_information: list[str] = Field(default_factory=list)
    planning_hypothesis: str | None = None


class CandidateSpotSummary(BaseModel):
    name: str
    area: str | None = None
    estimated_visit_duration_hours: float | None = None
    reason: str | None = None
    tags: list[str] = Field(default_factory=list)
    is_remote_heavy: bool = False
    is_rain_friendly: bool = False
    is_light_experience: bool = False


class CandidateCluster(BaseModel):
    cluster_id: str
    label: str
    selected_spots: list[str] = Field(default_factory=list)
    optional_spots: list[str] = Field(default_factory=list)
    rejected_spots: list[str] = Field(default_factory=list)
    why_it_works: str
    weather_fit: str | None = None
    effort_level: ClusterEffort = "balanced"
    night_closure_style: str | None = None
    must_stay_together: bool = False
    is_remote_day_candidate: bool = False

    @field_validator("effort_level", mode="before")
    @classmethod
    def normalize_effort_level(cls, value: str | None) -> ClusterEffort:
        normalized = str(value or "balanced").strip().lower()
        if normalized in {"light", "relaxed", "easy"}:
            return "light"
        if normalized in {"dense", "heavy", "packed"}:
            return "dense"
        return "balanced"


class ClusterPlanning(BaseModel):
    destination: str
    summary: str
    overall_rationale: str | None = None
    clusters: list[CandidateCluster] = Field(default_factory=list)
    rejected_spots_global: list[str] = Field(default_factory=list)
    needs_attraction_refresh: bool = False
    attraction_refresh_reason: str | None = None


class TransportCheckTarget(BaseModel):
    target_id: str
    from_label: str
    to_label: str
    reason: str
    day_index: int | None = None


class TransportCheckRequest(BaseModel):
    needs_transport_evidence: bool = False
    targets: list[TransportCheckTarget] = Field(default_factory=list)


class LodgingFitnessResult(BaseModel):
    anchor_status: LodgingAnchorStatus
    reason: str | None = None
    recommended_area_hint: str | None = None
    suggested_reanchor_strategy: str | None = None


class PlanningBudgets(BaseModel):
    attraction_refresh_remaining: int = 1
    lodging_refresh_remaining: int = 1
    transport_batch_remaining: int = 1
    render_repair_remaining: int = 1


class PlanningDaySkeleton(BaseModel):
    day_index: int
    primary_cluster_id: str | None = None
    secondary_cluster_id: str | None = None
    selected_spots: list[str] = Field(default_factory=list)
    optional_spots: list[str] = Field(default_factory=list)
    rejected_spots: list[str] = Field(default_factory=list)
    rationale: str
    pacing: PacingMode = "balanced"
    weather_strategy: str | None = None
    lunch_strategy: str | None = None
    night_closure_strategy: str | None = None
    return_strategy: str | None = None
    needs_transport_check: bool = False
    transport_check_targets: list[TransportCheckTarget] = Field(default_factory=list)
    needs_lodging_refresh_hint: bool = False

    @field_validator("pacing", mode="before")
    @classmethod
    def normalize_pacing(cls, value: str | None) -> PacingMode:
        normalized = str(value or "balanced").strip().lower()
        if normalized in {"relaxed", "light", "easy"}:
            return "relaxed"
        if normalized in {"dense", "heavy", "packed"}:
            return "dense"
        return "balanced"


class LodgingAnchorDecision(BaseModel):
    anchor_mode: AnchorMode
    anchor_name: str | None = None
    anchor_area: str | None = None
    reason: str


class PlanningSkeleton(BaseModel):
    destination: str
    summary: str
    overall_rationale: str | None = None
    selected_spots_global: list[str] = Field(default_factory=list)
    rejected_spots_global: list[str] = Field(default_factory=list)
    day_skeletons: list[PlanningDaySkeleton] = Field(default_factory=list)
    needs_transport_evidence: bool = False
    transport_check_request: TransportCheckRequest | None = None
    needs_lodging_refresh: bool = False
    lodging_refresh_reason: str | None = None

    @field_validator("needs_transport_evidence", "needs_lodging_refresh", mode="before")
    @classmethod
    def normalize_bool_flags(cls, value):
        if isinstance(value, bool):
            return value
        text = str(value or "").strip().lower()
        return text in {"true", "1", "yes", "y"}


class PlanLoopInput(BaseModel):
    request: dict
    collected_tools: list[str] = Field(default_factory=list)
    observation_log: list[dict] = Field(default_factory=list)
    current_hypothesis: str | None = None


class ClusterPlanInput(BaseModel):
    request: dict
    attraction_candidates: list[dict] = Field(default_factory=list)
    lodging_candidates: list[dict] = Field(default_factory=list)
    selected_lodging: dict | None = None
    weather: list[dict] = Field(default_factory=list)
    transport_evidence: list[dict] = Field(default_factory=list)
    observation_log: list[dict] = Field(default_factory=list)


class SkeletonPlanInput(BaseModel):
    request: dict
    cluster_plans: dict | None = None
    attraction_candidates: list[dict] = Field(default_factory=list)
    lodging_candidates: list[dict] = Field(default_factory=list)
    selected_lodging: dict | None = None
    selected_lodging_status: str | None = None
    weather: list[dict] = Field(default_factory=list)
    transport_evidence: list[dict] = Field(default_factory=list)
    planning_budgets: dict | None = None
    observation_log: list[dict] = Field(default_factory=list)


class FinalItineraryRenderInput(BaseModel):
    request: dict
    skeleton: dict
    weather: list[dict] = Field(default_factory=list)
    attraction_candidates: list[dict] = Field(default_factory=list)
    lodging_candidates: list[dict] = Field(default_factory=list)
    selected_lodging: dict | None = None
    planning_anchor: dict | None = None
    transport_evidence: list[dict] = Field(default_factory=list)


__all__ = [
    "AnchorMode",
    "CandidateCluster",
    "CandidateSpotSummary",
    "ClusterEffort",
    "ClusterPlanInput",
    "ClusterPlanning",
    "DialogueDecision",
    "FinalItineraryRenderInput",
    "LodgingAnchorDecision",
    "LodgingAnchorStatus",
    "LodgingFitnessResult",
    "PacingMode",
    "PlanInput",
    "PlanLoopInput",
    "PlannerStatus",
    "PlanningBudgets",
    "PlanningDaySkeleton",
    "PlanningNextAction",
    "PlanningRequest",
    "PlanningSkeleton",
    "SkeletonPlanInput",
    "ToolName",
    "TransportCheckRequest",
    "TransportCheckTarget",
    "TripPlan",
]
