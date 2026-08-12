from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


# 会话阶段枚举：尽量把“当前在做什么”说得更具体一点
ConversationStage = Literal[
    "collecting_destination",   # 还在补目的地
    "collecting_dates",         # 目的地已知，但还在补游玩日期
    "collecting_requirements",  # 目的地和日期已知后，继续补其他信息
    "clarification",            # 追问用户补齐关键信息
    "ready_to_plan",            # 关键字段齐了，准备进入规划
    "planning",                 # 正在生成规划
    "revise_collecting",        # 修改已有行程时，正在收集改稿信息
    "revise_ready",             # 改稿信息已基本齐备，准备进入修改
    "qa",                       # 当前是问答模式
    "completed",                # 当前会话目标已完成
    "closed",                   # 会话关闭
]

SessionIntentType = Literal["new_plan", "revise_plan", "clarification", "qa", "confirm", "reject", "end_session", "unknown"]
SessionRevisionScope = Literal["block_level", "day_level", "global"]


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


class SessionArtifactSummary(BaseModel):
    # 当前会话中已生成产物的轻量摘要（revise 判定依赖 plan_summary）
    plan_summary: dict[str, Any] | None = None

    # 产物状态是否存在，避免只靠 summary 空不空来判断
    has_current_plan: bool = False
    has_current_draft: bool = False

    # 产物版本/轮次信息，后续如果要做版本化可以继续扩展
    plan_revision_count: int = 0
    draft_revision_count: int = 0


class SessionMergeResult(BaseModel):
    # merge 前的完整状态
    previous_state: SessionRequestState

    # 本轮真正应用的 patch
    applied_patch: dict[str, Any] = Field(default_factory=dict)

    # merge 后的新完整状态
    next_state: SessionRequestState

    # 本轮发生变化的字段名
    changed_fields: list[str] = Field(default_factory=list)

    # merge 后仍然缺哪些关键字段
    remaining_missing_fields: list[str] = Field(default_factory=list)


class SessionState(BaseModel):
    session_id: str
    user_id: str | None = None

    # 当前会话处于哪个阶段
    conversation_stage: ConversationStage = "collecting_destination"

    # 截止当前轮的完整需求状态
    current_request_state: SessionRequestState = Field(default_factory=SessionRequestState)

    # 当前这一轮新增/修改的 patch
    current_turn_patch: dict[str, Any] = Field(default_factory=dict)

    # 最近一次 merge 后还缺哪些字段
    missing_fields_snapshot: list[str] = Field(default_factory=list)

    # 系统后续准备追问哪些字段
    pending_questions: list[str] = Field(default_factory=list)

    # 当前会话的产物摘要
    artifacts: SessionArtifactSummary = Field(default_factory=SessionArtifactSummary)

    # 最近几轮消息摘要，后续可给 intent 使用
    recent_messages: list[dict[str, str]] = Field(default_factory=list)

    revision_count: int = 0

    created_at: str | None = None
    updated_at: str | None = None


class SessionIntentResult(BaseModel):
    # 这里是 session 层消费的 intent 结果快照，不直接依赖 intent 模块 schema
    intent_type: SessionIntentType = "unknown"
    extracted_request_patch: dict[str, Any] = Field(default_factory=dict)
    revision_scope_hint: SessionRevisionScope | None = None
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
    session_context: dict[str, Any] = Field(default_factory=dict)
    artifacts: SessionArtifactSummary = Field(default_factory=SessionArtifactSummary)
    recent_messages: list[dict[str, str]] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)
