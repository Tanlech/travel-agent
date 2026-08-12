from __future__ import annotations

from pydantic import BaseModel, Field


class UserContext(BaseModel):
    preferred_styles: list[str] = Field(default_factory=list)
    disliked_styles: list[str] = Field(default_factory=list)
    accept_theme_park: bool | None = None
    accept_nightlife: bool | None = None
    pace_preference: str | None = None
    family_friendly: bool | None = None
    senior_friendly: bool | None = None
