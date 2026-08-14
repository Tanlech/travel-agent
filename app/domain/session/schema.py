from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.common.chat import ChatMessage
from app.domain.common.intent_type import IntentType, RevisionScope
from app.domain.common.session_context import SessionContextView
from app.domain.common.stage import ConversationStage
from app.domain.intent.schema import IntentPlanningRequest


class SessionRequestState(IntentPlanningRequest):
    """会话累计需求 = 意图层需求视图的扩展（字段与校验继承，消除双份定义漂移）。
    演进权衡：extra="forbid" 随继承生效，未来删字段需配套旧数据迁移。
    """
    pass

class SessionArtifactSummary(BaseModel):
    # 产物摘要（revise 判定依赖 plan_summary）
    plan_summary: dict[str, Any] | None = None
    # 产物（plan/draft）是否已落库：区分"从未生成"与"产物丢失"
    has_plan: bool = False
    # 最近一次产物写入时间（产物续期/一致性判断用）
    plan_updated_at: datetime | None = None

    @model_validator(mode="after")
    def _check_invariants(self) -> "SessionArtifactSummary":
        # 不变量：有产物必有写入时间，无产物不留摘要
        if self.has_plan and not self.plan_updated_at:
            raise ValueError("has_plan=True requires plan_updated_at")
        if not self.has_plan and self.plan_updated_at is not None:
            raise ValueError("has_plan=False requires plan_updated_at=None")
        return self


class SessionMergeResult(BaseModel):
    # merge 后的新完整状态
    next_state: SessionRequestState
    # merge 后仍缺的关键字段
    remaining_missing_fields: list[str] = Field(default_factory=list)
    # 本次实际应用的字段（值有变化才计入），审计/回显用
    changed_fields: list[str] = Field(default_factory=list)


class SessionState(BaseModel):
    session_id: str
    user_id: str | None = None
    # 乐观并发版本号：save 递增，版本不一致拒绝写入（调用方重新加载重试）
    version: int = Field(default=0, ge=0)
    # 当前会话阶段
    conversation_stage: ConversationStage = "collecting_destination"
    # 截止当前轮的完整需求状态
    current_request_state: SessionRequestState = Field(default_factory=SessionRequestState)
    # 下一步待追问的字段
    pending_questions: list[str] = Field(default_factory=list)
    # 当前会话产物摘要
    artifacts: SessionArtifactSummary = Field(default_factory=SessionArtifactSummary)
    # 最近几轮消息（供 intent 上下文）
    recent_messages: list[ChatMessage] = Field(default_factory=list)
    revision_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # 进入 closed 阶段的时间戳（生命周期审计/TTL 策略用）
    closed_at: datetime | None = None

    @field_validator("session_id")
    @classmethod
    def _non_empty_session_id(cls, v: str) -> str:
        # 空/纯空白 id 前置到 schema 拒绝，避免 repository 层才炸
        stripped = v.strip()
        if not stripped:
            raise ValueError("session_id must not be empty")
        return stripped

    @field_validator("user_id")
    @classmethod
    def _strip_user_id(cls, v: str | None) -> str | None:
        # 与 session_id 一致去空白；空串归一 None，避免空白 user 导致记忆分叉
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class SessionIntentResult(BaseModel):
    # session 层消费的 intent 结果快照（不依赖 intent 模块 schema）
    intent_type: IntentType = "unknown"
    extracted_request_patch: dict[str, Any] = Field(default_factory=dict)
    revision_scope_hint: RevisionScope | None = None
    missing_fields: list[str] = Field(default_factory=list)
    reasoning: str | None = None
    # 透传 intent 层丢弃的字段名（debug 留痕）
    patch_dropped_fields: list[str] = Field(default_factory=list)


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
