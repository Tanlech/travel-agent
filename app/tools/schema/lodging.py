from __future__ import annotations

from pydantic import BaseModel, Field


class LodgingCandidate(BaseModel):
    """提供给 Agent 的住宿候选基本信息"""
    poi_id: str | None = None
    name: str
    area: str | None = None
    price: str | None = None
    address: str | None = None
    tel: str | None = None


class LodgingInput(BaseModel):
    destination: str
    budget: int | None = None
    preferences: list[str] = Field(default_factory=list)
    avoid_keywords: list[str] = Field(default_factory=list)
    spots: list[str] = Field(default_factory=list)


class LodgingResult(BaseModel):
    city: str
    candidates: list[LodgingCandidate] = Field(default_factory=list)
    summary: str | None = None
