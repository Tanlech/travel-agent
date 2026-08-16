"""跨层共享的旅行需求与行程模型（单一来源，agents 层 re-export）"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.common.dates import normalize_date


# 必填关键字段（intent 追问 / session 空泛改稿判定共用，单一来源）
REQUIRED_PATCH_FIELDS = ("destination", "start_date", "end_date")


class TravelRequestFields(BaseModel):
    """旅行需求公共字段（intent/session 共用，patch 语义：全可选，字段单一来源）"""

    destination: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    days: int | None = None
    departure_city: str | None = None
    travelers: int | None = None  # 总人数
    preferences: list[str] = Field(default_factory=list)
    must_visit_spots: list[str] = Field(default_factory=list)
    optional_spots: list[str] = Field(default_factory=list)
    avoid_spots: list[str] = Field(default_factory=list)

    @field_validator("days", "travelers")
    @classmethod
    def _check_positive_int(cls, v: int | None) -> int | None:
        # 0/负值会污染下游 merge 与天数推导；None 表示未提供，放行
        if v is not None and v <= 0:
            raise ValueError("days/travelers must be positive integers")
        return v

    @model_validator(mode="after")
    def _normalize_dates(self) -> "TravelRequestFields":
        # 日期统一归一为 YYYY-MM-DD；end 早于 start 时对调
        for field in ("start_date", "end_date"):
            value = getattr(self, field)
            if not value:
                continue
            normalized = normalize_date(value)
            if normalized is None:
                raise ValueError(f"invalid {field} {value!r}, expected YYYY-MM-DD")
            setattr(self, field, normalized)
        if self.start_date and self.end_date and self.end_date < self.start_date:
            self.start_date, self.end_date = self.end_date, self.start_date
        return self


class PlanningRequest(BaseModel):
    """执行规划用的需求模型（agents 侧，destination/days 必填；travelers 为描述列表）"""

    destination: str
    days: int
    budget: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    departure_city: str | None = None
    revision_message: str | None = None
    travelers: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    must_visit_spots: list[str] = Field(default_factory=list)
    optional_spots: list[str] = Field(default_factory=list)
    avoid_spots: list[str] = Field(default_factory=list)

    @field_validator("days")
    @classmethod
    def normalize_days(cls, value: int) -> int:
        return max(1, int(value or 1))


class TripPlan(BaseModel):
    """对外行程结果（planning/revise 输出，daily_plan 等为自由 dict 结构）"""

    destination: str
    summary: str
    route_intent_summary: str | None = None
    daily_plan: list[dict] = Field(default_factory=list)
    stay_recommendation: list[dict] = Field(default_factory=list)
    transport_plan: list[dict] = Field(default_factory=list)
    weather_notes: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    reflection: Any | None = None


def compute_missing_fields(request: Any) -> list[str]:
    """返回必填关键字段（destination/start_date/end_date）中缺失的列表"""
    return [f for f in REQUIRED_PATCH_FIELDS if not str(getattr(request, f, None) or "").strip()]


def extract_plan_attractions(plan: Any) -> list[str]:
    """从行程（TripPlan 或 dict）的 daily_plan 提取最终采纳的景点标题（去重保序）"""
    daily_plan = getattr(plan, "daily_plan", None)
    if daily_plan is None and isinstance(plan, dict):
        daily_plan = plan.get("daily_plan") or []
    spots: list[str] = []
    for day in daily_plan or []:
        for item in day.get("items") or []:
            if item.get("item_type") == "attraction":
                title = str(item.get("title") or "").strip()
                if title and title not in spots:
                    spots.append(title)
    return spots
