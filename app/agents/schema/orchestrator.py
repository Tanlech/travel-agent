from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.agents.schema.planning import PlanningRequest


IntentType = Literal["new_plan", "revise_plan", "clarification", "qa", "confirm", "reject", "unknown"]
ExecutionMode = Literal["clarify", "planning", "revise", "qa"]
ExecutionStatus = Literal["completed", "pending", "failed", "needs_follow_up"]


class AgentRequest(BaseModel):
    request_id: str
    user_id: str | None = None
    session_id: str | None = None
    message: str = ""
    planning_request: PlanningRequest | None = None
    revision_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    debug: bool = False
    async_allowed: bool = True


class DialogueDecision(BaseModel):
    status: Literal["need_clarification", "ready_to_plan", "ready_to_revise"]
    missing_fields: list[str] = Field(default_factory=list)
    follow_up_question: str | None = None


class IntentParseResult(BaseModel):
    intent_type: IntentType = "unknown"
    confidence: float = 0.0
    normalized_planning_request: PlanningRequest | None = None
    revision_message: str | None = None
    revision_scope_hint: Literal["block_level", "day_level", "global"] | None = None
    missing_fields: list[str] = Field(default_factory=list)
    reasoning: str | None = None


class TokenBudgetPolicy(BaseModel):
    total_budget: int = 12000
    intent_budget: int = 800
    planning_budget: int = 5000
    revise_budget: int = 4000
    reflection_budget: int = 1500
    repair_budget: int = 1500


class TelemetryContext(BaseModel):
    trace_id: str | None = None
    task_id: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    token_usage: dict[str, int] = Field(default_factory=dict)
    latencies_ms: dict[str, int] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    mode: ExecutionMode
    run_reflection: bool = True
    run_repair: bool = True
    async_required: bool = False
    token_budget: int = 0
    reason: str | None = None


class AgentResponse(BaseModel):
    request_id: str
    session_id: str | None = None
    status: ExecutionStatus
    mode: ExecutionMode
    summary: str | None = None
    plan: dict[str, Any] | None = None
    draft: dict[str, Any] | None = None
    follow_up_question: str | None = None
    task_id: str | None = None
    trace_id: str | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    debug: dict[str, Any] = Field(default_factory=dict)


class OrchestratorDecisionTrace(BaseModel):
    request_mode: Literal["new_plan", "revise_plan", "clarification"]
    has_revision_message: bool = False
    has_session_artifacts: bool = False
    same_destination_as_previous: bool = False


__all__ = [
    "AgentRequest",
    "AgentResponse",
    "DialogueDecision",
    "ExecutionPlan",
    "IntentParseResult",
    "OrchestratorDecisionTrace",
    "TelemetryContext",
    "TokenBudgetPolicy",
]
