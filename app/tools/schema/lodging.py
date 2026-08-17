from __future__ import annotations

from pydantic import BaseModel, Field


class LodgingCandidate(BaseModel):
    poi_id: str | None = None
    name: str
    area: str | None = None
    source: str | None = None
    tags: list[str] = Field(default_factory=list)
    near_spot: bool = False  # 是否来自景点附近搜索（评分加权）
    # 高德 extensions=all 补全字段
    price: str | None = None  # 参考价格（如 "800"）
    rating: str | None = None  # 评分（1-5，如 "4.5"）
    star: str | None = None  # 星级（如 "4"、"5"）
    lng: float | None = None
    lat: float | None = None
    address: str | None = None
    tel: str | None = None


class SelectedLodging(BaseModel):
    poi_id: str | None = None
    name: str
    area: str | None = None
    source: str | None = None
    booking_note: str | None = None


class LodgingInput(BaseModel):
    destination: str  # 目的地（城市）
    budget: int | None = None  # 每晚预算（元），驱动档位词（≤300经济型 / ≤600舒适型 / ≤1500四星 / 更高五星）
    preferences: list[str] = Field(default_factory=list)  # 偏好（档位词如"四星/连锁"，或主题词如"亲子/商务"）
    avoid_spots: list[str] = Field(default_factory=list)  # 规避项（命中候选名称即剔除）
    spots: list[str] = Field(default_factory=list)  # 行程景点，驱动"附近酒店"搜索并加权


class LodgingResult(BaseModel):
    city: str
    candidates: list[LodgingCandidate] = Field(default_factory=list)
    selected_lodging: SelectedLodging | None = None
    summary: str | None = None
    source: str | None = None
    raw: dict | None = None
