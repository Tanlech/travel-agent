"""长期记忆数据模型：用户偏好 + 行程记忆"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field


class UserMemory(BaseModel):
    """跨会话累积的用户偏好（Redis mem:user:{id} 持久化）"""

    user_id: str | None = None  # None 表示匿名用户，不落盘
    preferred_styles: list[str] = Field(default_factory=list)  # 正向偏好（如"轻松""美食"）
    disliked_styles: list[str] = Field(default_factory=list)  # 负面偏好（如"紧凑"）
    accept_theme_park: bool | None = None  # None=未知，True=接受主题乐园
    accept_nightlife: bool | None = None  # None=未知，True=接受夜生活/演艺
    pace_preference: str | None = None  # relaxed / dense
    family_friendly: bool | None = None  # 是否适合带娃
    senior_friendly: bool | None = None  # 是否适合老人


class TripMemory(BaseModel):
    """一次已生成行程的快照（Redis mem:trip:{id} list，保留最近 MAX_TRIP_MEMORIES 条）"""

    destination: str
    days: int
    budget: int | None = None
    accepted_spots: list[str] = Field(default_factory=list)  # 规划采纳的景点
    rejected_spots: list[str] = Field(default_factory=list)  # 用户明确排除的景点
    summary: str | None = None
    response_mode: str | None = None  # 生成模式（final_plan / follow_up / revise_plan），非用户反馈
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
