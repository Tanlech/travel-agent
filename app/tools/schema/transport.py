from __future__ import annotations

from pydantic import BaseModel, Field


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


class TransitRoute(BaseModel):
    best_option: TransitOptionSummary | None = None


class TransportResult(BaseModel):
    city: str
    from_name: str | None = None
    to_name: str | None = None
    walk: WalkRoute | None = None
    taxi: TaxiRoute | None = None
    transit: TransitRoute | None = None
    error: str | None = None
    source: str | None = None
