"""会话上下文视图（session 投影给 intent 的轻量上下文）。"""

from pydantic import BaseModel

from app.domain.stage import ConversationStage


class SessionContextView(BaseModel):
    conversation_stage: ConversationStage = "collecting_destination"
    revision_count: int = 0
