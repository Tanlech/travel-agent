from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.common.stage import ConversationStage

"""会话视图（planning agent 内部只读；真实状态在 SessionState，经 mapper 桥接）"""


class SessionContext(BaseModel):
    """规划代理读取的会话快照（builder 兼容构造 / mapper 真实构造）"""

    session_id: str | None = None
    confirmed_fields: list[str] = Field(default_factory=list)  # 已确认需求字段
    pending_questions: list[str] = Field(default_factory=list)  # 待追问字段
    conversation_stage: ConversationStage = "collecting_destination"
    last_destination: str | None = None
    revision_count: int = 0
