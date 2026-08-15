"""跨层共享的会话视图模型（SessionContext 完整版 + SessionContextView 轻量版）"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain.common.stage import ConversationStage

# 已确认需求字段的判定口径（与 REQUIRED_PATCH_FIELDS 对齐，顺序 = 展示顺序）
_CONFIRMABLE_FIELDS = ("destination", "start_date", "end_date")


def compute_confirmed_fields(request: object) -> list[str]:
    """从累计需求对象推导已确认字段（任一非空即确认）。

    供 SessionState / PlanningRequest 两类累计需求共用，避免 builder/mapper 各写一份判定。
    """
    return [field for field in _CONFIRMABLE_FIELDS if getattr(request, field, None)]


class SessionContext(BaseModel):
    """规划/改稿代理读取的会话快照（真实状态在 SessionState，经 mapper 桥接）"""

    session_id: str | None = None
    confirmed_fields: list[str] = Field(default_factory=list)  # 已确认需求字段
    pending_questions: list[str] = Field(default_factory=list)  # 待追问字段
    conversation_stage: ConversationStage = "collecting_destination"
    last_destination: str | None = None
    revision_count: int = 0


class SessionContextView(BaseModel):
    """session 投影给 intent 的轻量上下文（只含意图判定所需字段）"""

    conversation_stage: ConversationStage = "collecting_destination"
    revision_count: int = 0
