from __future__ import annotations

from pydantic import BaseModel, Field


class LodgingCandidate(BaseModel):
    """提供给 Agent 的住宿候选基本信息"""
    poi_id: str | None = None
    name: str
    area: str | None = None
    address: str | None = None
    tel: str | None = None
    rating: str | None = Field(default=None, description="高德评分，如 4.7；缺失表示无评分")
    keytag: str | None = Field(default=None, description="高德档次标签，如 四星级酒店/豪华型/经济型/青年旅舍")
    distance_to_spots_km: float | None = Field(default=None, description="距最近行程景点的距离（公里）")


class LodgingInput(BaseModel):
    """住宿检索输入：给定目的地与约束，返回真实可预订的住宿候选供 Agent 选择"""
    destination: str = Field(description="目的地城市，必填")
    preferences: list[str] = Field(
        default_factory=list,
        description="住宿偏好，如['市中心', '四星', '亲子', '商务']；含档次词(星/经济型/舒适/豪华/高档/快捷)或主观词(交通方便/干净/安静/位置好)会被用于查询与排序",
    )
    avoid_keywords: list[str] = Field(default_factory=list, description="用户明确不想要的住宿类型关键词，如['民宿', '招待所']")
    spots: list[str] = Field(
        default_factory=list,
        description="行程中希望住宿靠近的景点名称（同城），用于按距离排序，最多取前4个",
    )
    top_n: int = Field(default=5, description="返回的候选数量上限，默认 5")


class LodgingResult(BaseModel):
    city: str
    candidates: list[LodgingCandidate] = Field(default_factory=list)
    summary: str | None = None
    debug: dict | None = Field(default=None, description="可观测性统计：查询数/原始候选数/过滤后数量/无评分数量")
