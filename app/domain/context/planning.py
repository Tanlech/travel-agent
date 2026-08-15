from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.common.itinerary import ItineraryDraftSchema
from app.domain.common.planning import PlanningRequest, TripPlan
from app.domain.common.reflection import ReflectionResult
from app.domain.common.session_context import SessionContext
from app.domain.common.user import UserContext
from app.tools.schema.attraction import AttractionResult
from app.tools.schema.lodging import LodgingResult, SelectedLodging
from app.tools.schema.transport import TransportResult
from app.tools.schema.weather import WeatherResult

"""规划全流程状态容器（planning / repair / reflection / revise 共享传递）"""


class PlanningContext(BaseModel):
    """一次规划任务的完整状态：请求 + 用户/会话视图 + 工具结果 + 中间产物"""

    request: PlanningRequest
    user: UserContext = Field(default_factory=UserContext)
    session: SessionContext = Field(default_factory=SessionContext)
    weather_result: WeatherResult | None = None
    attraction_result: AttractionResult | None = None
    lodging_result: LodgingResult | None = None
    selected_lodging: SelectedLodging | None = None
    transport_results: list[TransportResult] = Field(default_factory=list)
    draft: ItineraryDraftSchema | None = None  # 行程稿（中间产物）
    plan: TripPlan | None = None  # 对外行程结果
    reflection_result: ReflectionResult | None = None
    status: str = "initialized"  # initialized / planning / completed
    revision_count: int = 0
    trace: list[dict] = Field(default_factory=list)  # 步骤日志
