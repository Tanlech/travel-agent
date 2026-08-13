from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UserMemory(BaseModel):
    user_id: str | None = None
    preferred_styles: list[str] = Field(default_factory=list)
    disliked_styles: list[str] = Field(default_factory=list)
    accept_theme_park: bool | None = None
    accept_nightlife: bool | None = None
    pace_preference: str | None = None
    family_friendly: bool | None = None
    senior_friendly: bool | None = None


class TripMemory(BaseModel):
    destination: str
    days: int
    budget: int | None = None
    accepted_spots: list[str] = Field(default_factory=list)
    rejected_spots: list[str] = Field(default_factory=list)
    summary: str | None = None
    feedback: str | None = None
