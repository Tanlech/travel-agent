from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.domain.stage import ConversationStage


class UserMemory(BaseModel):
    user_id: str | None = None
    preferred_styles: list[str] = Field(default_factory=list)
    disliked_styles: list[str] = Field(default_factory=list)
    accept_theme_park: bool | None = None
    accept_nightlife: bool | None = None
    pace_preference: str | None = None
    family_friendly: bool | None = None
    senior_friendly: bool | None = None


class SessionMemory(BaseModel):
    session_id: str | None = None
    confirmed_fields: list[str] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
    conversation_stage: ConversationStage = "collecting_destination"
    last_destination: str | None = None
    revision_count: int = 0
    current_plan: dict[str, Any] | None = None
    current_draft: dict[str, Any] | None = None


class TripMemory(BaseModel):
    destination: str
    days: int
    budget: int | None = None
    accepted_spots: list[str] = Field(default_factory=list)
    rejected_spots: list[str] = Field(default_factory=list)
    summary: str | None = None
    feedback: str | None = None
