from __future__ import annotations

from pydantic import BaseModel, Field

from app.agents.schema import ReflectionResult, TripPlan
from app.domain.context.session import SessionContext
from app.domain.context.user import UserContext
from app.infrastructure.llm.schemas import ItineraryDraftSchema
from app.agents.schema.planning import PlanningRequest
from app.tools.schema.attraction import AttractionResult
from app.tools.schema.lodging import LodgingResult, SelectedLodging
from app.tools.schema.transport import TransportResult
from app.tools.schema.weather import WeatherResult


class PlanningContext(BaseModel):
    request: PlanningRequest
    user: UserContext = Field(default_factory=UserContext)
    session: SessionContext = Field(default_factory=SessionContext)
    weather_result: WeatherResult | None = None
    attraction_result: AttractionResult | None = None
    lodging_result: LodgingResult | None = None
    selected_lodging: SelectedLodging | None = None
    transport_results: list[TransportResult] = Field(default_factory=list)
    draft: ItineraryDraftSchema | None = None
    plan: TripPlan | None = None
    reflection_result: ReflectionResult | None = None
    status: str = "initialized"
    revision_count: int = 0
    trace: list[dict] = Field(default_factory=list)
