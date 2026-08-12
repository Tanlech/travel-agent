from __future__ import annotations

from pydantic import BaseModel, Field


class LodgingCandidate(BaseModel):
    poi_id: str | None = None
    name: str
    area: str | None = None
    source: str | None = None
    tags: list[str] = Field(default_factory=list)


class SelectedLodging(BaseModel):
    poi_id: str | None = None
    name: str
    area: str | None = None
    source: str | None = None
    booking_note: str | None = None


class LodgingInput(BaseModel):
    destination: str
    budget: int | None = None
    preferences: list[str] = Field(default_factory=list)
    avoid_spots: list[str] = Field(default_factory=list)
    spots: list[str] = Field(default_factory=list)


class LodgingResult(BaseModel):
    city: str
    candidates: list[LodgingCandidate] = Field(default_factory=list)
    selected_lodging: SelectedLodging | None = None
    summary: str | None = None
    source: str | None = None
    raw: dict | None = None
