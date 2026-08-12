from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class AttractionCandidate(BaseModel):
    poi_id: str | None = None
    name: str
    area: str | None = None
    source: str | None = None
    estimated_visit_duration_hours: float | None = None
    reason: str | None = None
    entity_level: str | None = None
    tags: list[str] = Field(default_factory=list)


class AttractionInput(BaseModel):
    city: str
    days: int
    must_visit_spots: list[str] = Field(default_factory=list)
    avoid_spots: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    existing_candidates: list[AttractionCandidate] = Field(default_factory=list)
    target_count: int | None = None
    target_count_min: int | None = None
    target_count_max: int | None = None

    @field_validator("existing_candidates", mode="before")
    @classmethod
    def normalize_existing_candidates(cls, value):
        if value is None:
            return []
        normalized: list[AttractionCandidate | dict] = []
        for item in value:
            if isinstance(item, AttractionCandidate):
                normalized.append(item)
            elif isinstance(item, str):
                name = item.strip()
                if name:
                    normalized.append({"name": name})
            elif isinstance(item, dict):
                normalized.append(item)
        return normalized


class AttractionResult(BaseModel):
    city: str
    candidates: list[AttractionCandidate] = Field(default_factory=list)
    must_visit_verified: list[AttractionCandidate] = Field(default_factory=list)
    avoid_verified: list[AttractionCandidate] = Field(default_factory=list)
    source: str | None = None
    raw: dict | None = None
