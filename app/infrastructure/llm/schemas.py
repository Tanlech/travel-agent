from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.common.itinerary import (
    ItineraryDayPlan,
    ItineraryDraftSchema,
    ItinerarySpotRefSchema,
    ItineraryTimeBlockSchema,
)

# 行程稿模型（ItineraryDraftSchema 等）定义在 domain.common.itinerary，此处 re-export 保持旧引用可用


class RepairDayPlanSchema(BaseModel):
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
