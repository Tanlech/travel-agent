from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class ItinerarySpotRefSchema(BaseModel):
    candidate_index: int | None = None
    poi_id: str | None = None
    reason: str | None = None


class ItineraryTimeBlockSchema(BaseModel):
    start_time: str
    end_time: str
    item_type: str
    title: str
    detail: str | None = None
    area: str | None = None
    estimated_cost: float | None = None


class ItineraryDayPlan(BaseModel):
    day_index: int
    primary_area: str | None = None
    time_blocks: list[ItineraryTimeBlockSchema] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_block_mix(self) -> "ItineraryDayPlan":
        item_types = [block.item_type for block in self.time_blocks]
        attraction_count = sum(1 for item_type in item_types if item_type == "attraction")
        transport_count = sum(1 for item_type in item_types if item_type == "transport")
        meal_count = sum(1 for item_type in item_types if item_type == "meal")
        end_block_count = sum(1 for item_type in item_types if item_type in {"return", "flex"})
        if self.time_blocks and attraction_count < 1:
            raise ValueError("Each day must include at least one attraction block.")
        if self.time_blocks and transport_count < 1:
            raise ValueError("Each day must include at least one transport block.")
        if self.time_blocks and meal_count < 1:
            raise ValueError("Each day must include at least one meal block.")
        if self.time_blocks and end_block_count < 1:
            raise ValueError("Each day must include a return or flex block.")
        return self


class ItineraryDraftSchema(BaseModel):
    destination: str
    summary: str
    route_intent_summary: str | None = None
    selected_day_areas: list[str] = Field(default_factory=list)
    day_plans: list[ItineraryDayPlan] = Field(default_factory=list)


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
