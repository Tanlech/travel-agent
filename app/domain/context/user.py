from __future__ import annotations

from pydantic import BaseModel, Field

"""用户画像上下文（memory 融合结果，供规划/改稿代理读取）"""


class UserContext(BaseModel):
    """规划时用的用户偏好快照（字段与 UserMemory 对齐，来源 memory_manager.build_user_context）"""

    preferred_styles: list[str] = Field(default_factory=list)  # 正向偏好
    disliked_styles: list[str] = Field(default_factory=list)  # 负面偏好
    accept_theme_park: bool | None = None  # None=未知
    accept_nightlife: bool | None = None
    pace_preference: str | None = None  # relaxed / dense
    family_friendly: bool | None = None
    senior_friendly: bool | None = None
