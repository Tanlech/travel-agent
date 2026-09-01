from __future__ import annotations

from pydantic import BaseModel, Field


class MealCandidate(BaseModel):
    """提供给 Agent 的真实餐饮候选基本信息"""
    poi_id: str | None = None
    name: str
    area: str | None = None
    address: str | None = None
    lng: float | None = Field(default=None, description="高德经度，用于地图标点/路线回填")
    lat: float | None = Field(default=None, description="高德纬度，用于地图标点/路线回填")
    rating: str | None = Field(default=None, description="高德评分，如 4.6；缺失表示无评分")
    type_name: str | None = Field(default=None, description="高德餐饮类别，如 火锅/中餐厅/小吃")
    distance_to_spots_km: float | None = Field(default=None, description="距最近行程景点的距离（公里）")


class MealInput(BaseModel):
    """餐饮检索输入：给定目的地与饮食偏好，返回真实餐馆候选供 Agent 排进行程"""
    destination: str = Field(description="目的地城市，必填")
    preferences: list[str] = Field(
        default_factory=list,
        description="饮食偏好，如['火锅', '本地菜', '清淡', '海鲜']；用于命中餐饮类别与排序",
    )
    spots: list[str] = Field(
        default_factory=list,
        description="行程中希望就近用餐的景点名称（同城），用于按距离排序，最多取前4个",
    )
    top_n: int = Field(default=8, description="返回的候选数量上限，默认 8")


class MealResult(BaseModel):
    city: str
    candidates: list[MealCandidate] = Field(default_factory=list)
    summary: str | None = None
    debug: dict | None = Field(default=None, description="可观测性统计：查询数/原始候选数/过滤后数量")