from __future__ import annotations

from pydantic import BaseModel, Field


class TransportInput(BaseModel):
    """交通路线查询输入：查询同城两点或多点间的交通方案（步行/打车/公交）"""
    city: str = Field(description="城市，必填")
    from_name: str | None = Field(default=None, description="起点地点名称，如'广州塔'；不填则无法查询")
    to_name: str | None = Field(default=None, description="终点地点名称，如'孙中山纪念堂'；不填则无法查询")
    waypoints: list[str] = Field(
        default_factory=list,
        description="可选途经点列表（同城），按顺序连接起点与终点，每段独立查询；最多支持 5 段",
    )


class WalkRoute(BaseModel):
    distance_meters: int | None = None
    duration_minutes: int | None = None


class TaxiRoute(BaseModel):
    cost: float | None = None
    distance_meters: int | None = None
    duration_minutes: int | None = None


class TransitRouteStep(BaseModel):
    mode: str | None = None
    distance_meters: int | None = None
    duration_minutes: int | None = None
    name: str | None = None
    from_station: str | None = None
    to_station: str | None = None


class TransitOptionSummary(BaseModel):
    cost: float | None = None
    duration_minutes: int | None = None
    distance_meters: int | None = None
    walking_distance_meters: int | None = None
    transfer_count: int | None = None
    steps: list[TransitRouteStep] = Field(default_factory=list)


class TransportResult(BaseModel):
    city: str
    from_name: str | None = None
    to_name: str | None = None
    walk: WalkRoute | None = None
    taxi: TaxiRoute | None = None
    transit: TransitOptionSummary | None = None
    error: str | None = None
    note: str | None = None
    source: str | None = None
