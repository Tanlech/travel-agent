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
    """景点检索输入：检索某城市的代表性景点候选，供行程规划选点"""
    city: str = Field(description="目的地城市，必填")
    days: int = Field(description="行程天数，用于控制候选数量与规划规模")
    must_visit_spots: list[str] = Field(default_factory=list, description="用户必去景点名称列表，会优先保留并校验真实性")
    avoid_spots: list[str] = Field(default_factory=list, description="用户明确不去的景点名称列表，会从结果中排除")
    preferences: list[str] = Field(
        default_factory=list,
        description="游玩主题偏好，如['历史文化', '自然景观', '亲子', '博物馆']，用于生成搜索查询",
    )
    existing_candidates: list[AttractionCandidate] = Field(
        default_factory=list,
        description="已存在的候选景点（如改稿时的旧候选），用于增量补充避免重复推荐",
    )
    target_count: int | None = Field(default=None, description="目标候选数量（与 min/max 二选一）")
    target_count_min: int | None = Field(default=None, description="目标候选数量下限，如 8")
    target_count_max: int | None = Field(default=None, description="目标候选数量上限，如 12")

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
