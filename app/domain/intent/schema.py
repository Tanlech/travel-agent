from __future__ import annotations

from typing import Any, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.common.chat import ChatMessage
from app.domain.common.dates import normalize_date
from app.domain.common.intent_type import IntentType, RevisionScope
from app.domain.common.session_context import SessionContextView


class IntentPlanningRequest(BaseModel):
    """intent 层旅行需求视图，与 session 层 SessionRequestState 由 adapter 互转（extra="forbid" 防字段漂移）"""

    model_config = ConfigDict(extra="forbid")

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
    def _normalize_dates(self) -> "IntentPlanningRequest":
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


class IntentRecognitionInput(BaseModel):
    """意图识别输入：用户原话 + 当前会话上下文"""

    request_id: str
    session_id: str | None = None
    user_id: str | None = None

    raw_message: str = Field(min_length=1)  # 用户原话，空白直接拒绝
    planning_request: IntentPlanningRequest | None = None  # 当前结构化需求（上下文）
    session_context: SessionContextView = Field(default_factory=SessionContextView)  # 会话状态
    user_context: dict[str, Any] = Field(default_factory=dict)  # 用户长期偏好
    latest_plan_summary: dict[str, Any] | None = None  # 已有行程摘要（revise 判定依据）
    has_plan: bool = False  # 会话层产物落库标记（更可靠的"已有行程"判据）
    recent_messages: list[ChatMessage] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)

    @field_validator("raw_message")
    @classmethod
    def _strip_blank_message(cls, v: str) -> str:
        # 去首尾空白并拒绝纯空白消息
        stripped = v.strip()
        if not stripped:
            raise ValueError("raw_message must not be blank")
        return stripped


class IntentRecognitionOutput(BaseModel):
    """意图识别输出。patch-only：只返回本轮新增/修改字段，累计合并由 session 层负责。"""

    intent_type: IntentType = IntentType.UNKNOWN
    extracted_request_patch: dict[str, Any] = Field(default_factory=dict)  # 本轮新增/修改的字段
    revision_scope_hint: RevisionScope | None = None  # 改动范围（仅 revise 有意义）
    missing_fields: list[str] = Field(default_factory=list)  # 仍缺的关键字段
    reasoning: str | None = None  # 调试/回溯用，不直接展示给用户
    patch_dropped_fields: list[str] = Field(default_factory=list)  # 校验被丢弃的字段，诊断留痕

    @field_validator("missing_fields")
    @classmethod
    def _sanitize_missing(cls, v: list[str]) -> list[str]:
        # 只允许必填字段进入追问列表；白名单过滤 + 保序去重
        seen: list[str] = []
        for f in v:
            if f in REQUIRED_PATCH_FIELDS and f not in seen:
                seen.append(f)
        return seen

    @field_validator("revision_scope_hint", mode="before")
    @classmethod
    def _sanitize_scope(cls, v: str | None) -> str | None:
        # 枚举外值归一为 None（revise 分支兜底 day_level）；合法值来源与 RevisionScope 单一
        if v in get_args(RevisionScope):
            return v
        return None

    @model_validator(mode="after")
    def _sanitize_output(self) -> "IntentRecognitionOutput":
        # 1. patch 归一化 + 丢弃留痕；patch 已提供的字段不再列为缺失
        cleaned, dropped = _clean_patch(self.extracted_request_patch)
        self.extracted_request_patch = cleaned
        if dropped:
            self.patch_dropped_fields = sorted(set(self.patch_dropped_fields) | set(dropped))
        if self.missing_fields:
            self.missing_fields = [f for f in self.missing_fields if f not in self.extracted_request_patch]

        # 2. revise：scope 缺省给 day_level（最局部、最安全）
        if self.intent_type == IntentType.REVISE_PLAN:
            if self.revision_scope_hint is None:
                self.revision_scope_hint = "day_level"

        # 3. clarification 却无缺失字段 → 字段其实齐了，降级 new_plan（session 层还会按真实缺失复核）
        if self.intent_type == IntentType.CLARIFICATION and not self.missing_fields:
            self.intent_type = IntentType.NEW_PLAN
            self.reasoning = (
                f"{self.reasoning or ''}（归一：clarification 无缺失字段，降级为 new_plan）"
            ).strip()

        # 4. 一致性：scope 只属于 revise；纯对话/收尾类意图清空 patch 与缺失字段
        if self.intent_type != IntentType.REVISE_PLAN:
            self.revision_scope_hint = None
        if self.intent_type in (
            IntentType.QA,
            IntentType.CONFIRM,
            IntentType.REJECT,
            IntentType.END_SESSION,
            IntentType.UNKNOWN,
        ):
            self.extracted_request_patch = {}
            self.missing_fields = []
        return self


# ============================================================
# 辅助逻辑：patch 清洗（供上述模型校验使用）
# ============================================================


def _annotation_is_int(annotation: Any) -> bool:
    """判断字段注解是否为 int（兼容 int | None 可选写法）"""
    if annotation is int:
        return True
    if get_origin(annotation) is not None:
        return int in get_args(annotation)
    return False


# 白名单与类型集合均派生自 IntentPlanningRequest（字段单一来源）
_ALLOWED_PATCH_FIELDS = set(IntentPlanningRequest.model_fields)
_LIST_PATCH_FIELDS = {
    name for name, field in IntentPlanningRequest.model_fields.items() if get_origin(field.annotation) is list
}
_INT_PATCH_FIELDS = {
    name for name, field in IntentPlanningRequest.model_fields.items() if _annotation_is_int(field.annotation)
}
# 必须为正整数的字段（0/负值会产生异常下游状态）
_POSITIVE_INT_FIELDS = {"days", "travelers"}
# 必填关键字段（有序：追问顺序 = 定义顺序）；session 层直接引用
REQUIRED_PATCH_FIELDS = ("destination", "start_date", "end_date")


def _clean_patch(patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """patch 归一化：白名单过滤 + 按字段类型归一；返回 (cleaned, dropped)"""
    cleaned: dict[str, Any] = {}
    dropped: list[str] = []
    for field, value in patch.items():
        if field not in _ALLOWED_PATCH_FIELDS:
            dropped.append(field)
            continue
        normalized = _clean_field_value(field, value)
        if normalized is None:
            dropped.append(field)
        else:
            cleaned[field] = normalized

    # 成对日期为权威：方向修正 + 清 days（天数由区间派生；单端日期+days 的补全交给 session merge）
    if "start_date" in cleaned and "end_date" in cleaned:
        if cleaned["start_date"] > cleaned["end_date"]:
            cleaned["start_date"], cleaned["end_date"] = cleaned["end_date"], cleaned["start_date"]
        cleaned.pop("days", None)
    return cleaned, dropped


def _clean_field_value(field: str, value: Any) -> Any | None:
    """按字段类型归一单个 patch 值；非法值返回 None（由调用方记入 dropped）"""
    # 日期：尽力归一为 YYYY-MM-DD
    if field in ("start_date", "end_date"):
        return normalize_date(value) if isinstance(value, str) else None
    # 列表：字符串按单元素包裹；只保留非空字符串元素并去重（保序）
    if field in _LIST_PATCH_FIELDS:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return None
        items: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                stripped = item.strip()
                if stripped not in items:
                    items.append(stripped)
        return items or None
    # 整数：兼容数字字符串与整数 float（如 3.0）；days/travelers 必须为正整数
    if field in _INT_PATCH_FIELDS:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        if isinstance(value, float) and not value.is_integer():
            return None
        try:
            int_value = int(value)
        except (ValueError, TypeError):
            return None
        if field in _POSITIVE_INT_FIELDS and int_value <= 0:
            return None
        return int_value
    # 标量字符串：去空白，空串视为非法
    if isinstance(value, str):
        return value.strip() or None
    return None
