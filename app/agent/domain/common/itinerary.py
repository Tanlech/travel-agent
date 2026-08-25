"""跨层共享的行程稿模型（LLM 结构化输出 schema，infrastructure 层 re-export）"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class ItinerarySpotRefSchema(BaseModel):
    candidate_index: int | None = None
    poi_id: str | None = None
    reason: str | None = None


class ItineraryTimeBlockSchema(BaseModel):
    start_time: str
    end_time: str
    item_type: str
    title: str
    detail: str | None = None
    area: str | None = None
    estimated_cost: float | None = None


class ItineraryDayPlan(BaseModel):
    day_index: int
    primary_area: str | None = None
    time_blocks: list[ItineraryTimeBlockSchema] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_block_mix(self) -> "ItineraryDayPlan":
        # 只在“完全没有内容块”时拦截空天；不再在解析边界硬校验“每天必须含景点/交通/餐饮/收尾”。
        # 原因：LLM 结构化输出任何一个 day_plan 缺了某类块，都会让整篇 ItineraryDraftSchema 解析失败，
        # 从而丢掉其它正常的日期。这类完备性保证已交由收敛修复层去兜底
        # （reflection 的 sparse_day/missing_meal_block/missing_return_block → repair 用真实候选点重建当天）。
        return self


class ItineraryDraftSchema(BaseModel):
    destination: str
    summary: str
    route_intent_summary: str | None = None
    selected_day_areas: list[str] = Field(default_factory=list)
    day_plans: list[ItineraryDayPlan] = Field(default_factory=list)
