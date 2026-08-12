from __future__ import annotations

from pydantic import BaseModel, Field


class ResponseContext(BaseModel):
    response_mode: str = "final_plan"
    include_alternatives: bool = True
    include_detailed_reasoning: bool = False
    include_summary: bool = True
    include_daily_plan: bool = True
    include_stay_recommendation: bool = True
    include_transport_plan: bool = True
    include_weather_notes: bool = True
    needs_follow_up: bool = False
    audience_notes: list[str] = Field(default_factory=list)
