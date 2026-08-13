from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.stage import ConversationStage


class SessionContext(BaseModel):
    session_id: str | None = None
    confirmed_fields: list[str] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
    conversation_stage: ConversationStage = "collecting_destination"
    last_destination: str | None = None
    revision_count: int = 0
