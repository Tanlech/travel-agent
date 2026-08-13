from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.intent.schema import ChatMessage, normalize_date
from app.domain.intent_type import IntentType, RevisionScope
from app.domain.session_context import SessionContextView
from app.domain.stage import ConversationStage


class SessionRequestState(BaseModel):
    # 当前会话已累计得到的完整需求状态
    destination: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    days: int | None = None
    departure_city: str | None = None

    # travelers 表示总人数，不再区分老人/小孩等标签
    travelers: int | None = None

    preferences: list[str] = Field(default_factory=list)
    must_visit_spots: list[str] = Field(default_factory=list)
    optional_spots: list[str] = Field(default_factory=list)
    avoid_spots: list[str] = Field(default_factory=list)

    @field_validator("days", "travelers")
    @classmethod
    def _check_positive_int(cls, v: int | None) -> int | None:
        # 与 intent 层 IntentPlanningRequest 口径一致：0/负值会污染下游状态
        if v is not None and v <= 0:
            raise ValueError("days/travelers must be positive integers")
        return v

    @model_validator(mode="after")
    def _normalize_dates(self) -> "SessionRequestState":
        # 复用 intent 层 normalize_date，统一日期合法性 + 跨年校验口径
        for field in ("start_date", "end_date"):
            value = getattr(self, field)
            if not value:
                continue
            normalized = normalize_date(value)
            if normalized is None:
                raise ValueError(f"invalid {field} {value!r}, expected YYYY-MM-DD")
            setattr(self, field, normalized)
        # 区间方向修正：end 早于 start（历史脏数据）时对调
        if self.start_date and self.end_date and self.end_date < self.start_date:
            self.start_date, self.end_date = self.end_date, self.start_date
        return self


class SessionArtifactSummary(BaseModel):
    # 当前会话中已生成产物的轻量摘要（revise 判定依赖 plan_summary）
    plan_summary: dict[str, Any] | None = None


class SessionMergeResult(BaseModel):
    # merge 后的新完整状态
    next_state: SessionRequestState

    # merge 后仍然缺哪些关键字段
    remaining_missing_fields: list[str] = Field(default_factory=list)


class SessionState(BaseModel):
    session_id: str
    user_id: str | None = None

    # 当前会话处于哪个阶段
    conversation_stage: ConversationStage = "collecting_destination"

    # 截止当前轮的完整需求状态
    current_request_state: SessionRequestState = Field(default_factory=SessionRequestState)

    # 系统后续准备追问哪些字段
    pending_questions: list[str] = Field(default_factory=list)

    # 当前会话的产物摘要
    artifacts: SessionArtifactSummary = Field(default_factory=SessionArtifactSummary)

    # 最近几轮消息摘要，后续可给 intent 使用
    recent_messages: list[ChatMessage] = Field(default_factory=list)

    revision_count: int = 0

    created_at: str | None = None
    updated_at: str | None = None


class SessionIntentResult(BaseModel):
    # 这里是 session 层消费的 intent 结果快照，不直接依赖 intent 模块 schema
    intent_type: IntentType = "unknown"
    extracted_request_patch: dict[str, Any] = Field(default_factory=dict)
    revision_scope_hint: RevisionScope | None = None
    missing_fields: list[str] = Field(default_factory=list)
    should_load_existing_artifacts: bool = False
    reasoning: str | None = None


class SessionApplyIntentResult(BaseModel):
    # 应用 intent 结果后的最新 session
    session_state: SessionState

    # 本次状态合并结果
    merge_result: SessionMergeResult

    # 原始 intent 结果快照，便于后续链路继续消费
    intent_result: SessionIntentResult


class SessionIntentView(BaseModel):
    # 从 session 层投影给 intent 层的上下文视图
    planning_request: dict[str, Any] = Field(default_factory=dict)
    session_context: SessionContextView = Field(default_factory=SessionContextView)
    artifacts: SessionArtifactSummary = Field(default_factory=SessionArtifactSummary)
    recent_messages: list[ChatMessage] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
