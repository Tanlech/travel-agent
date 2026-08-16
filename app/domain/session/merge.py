from __future__ import annotations

from datetime import date, timedelta
from typing import Any, get_origin

from app.domain.common.planning import REQUIRED_PATCH_FIELDS, compute_missing_fields
from app.domain.session.schema import SessionMergeResult, SessionRequestState


# 白名单/类型集合从 SessionRequestState 派生（字段单一来源，新增字段无需维护第二份）
ALLOWED_PATCH_FIELDS = set(SessionRequestState.model_fields)

LIST_FIELDS = {
    name for name, field in SessionRequestState.model_fields.items() if get_origin(field.annotation) is list
}

# 必填关键字段（有序 = 追问顺序；与 intent 层单一来源）
REQUIRED_FIELDS = REQUIRED_PATCH_FIELDS

# 合并规则：白名单才参与；None/空串不覆盖；标量覆盖、列表去重合并

def merge_request_state(
    current_state: SessionRequestState | None,
    request_patch: dict[str, Any],
) -> SessionMergeResult:
    # 以旧状态为基底（无旧状态时用空状态），在副本上累加
    next_dump = dict((current_state or SessionRequestState()).model_dump())
    changed_fields: list[str] = []

    # 逐字段合并 patch —— 这是 patch-only 累计的核心
    for field, value in request_patch.items():
        # 规则1：白名单外字段直接跳过（防止 LLM 输出意外字段污染状态）
        if field not in ALLOWED_PATCH_FIELDS:
            continue
        # 规则2：None 不覆盖（区分"未提供"和"显式清空"）
        if value is None:
            continue
        # 规则3：空字符串不覆盖（同上）
        if isinstance(value, str) and not value.strip():
            continue

        # 列表字段强制转 list（防字符串拆字）
        if field in LIST_FIELDS:
            normalized_value = value if isinstance(value, list) else [value]
            # 合并语义（patch-only）：intent 层只输出本轮新增项（prompt 明确"不输出完整 request"）
            # 去重追加到存量后；空列表/无新增视为未提供，不覆盖
            base_items = [i for i in (next_dump.get(field) or []) if i]
            merged_items = list(dict.fromkeys(base_items + [i for i in normalized_value if i]))
            if merged_items != base_items:
                next_dump[field] = merged_items
                changed_fields.append(field)
            continue
        if next_dump.get(field) != normalized_value:
            next_dump[field] = normalized_value
            changed_fields.append(field)

    # 用合并后的 dict 重建强类型对象（会做字段校验）
    next_state = SessionRequestState(**next_dump)
    # 日期区间与天数归一化（如"8月10号玩3天"补全 end_date）
    before_dates = (next_state.start_date, next_state.end_date, next_state.days)
    next_state = _normalize_date_range(next_state, request_patch)
    # 归一化推导出的日期变化也记入 changed_fields（如 days → 补全 end_date）
    for field, before, after in zip(
        ("start_date", "end_date", "days"),
        before_dates,
        (next_state.start_date, next_state.end_date, next_state.days),
    ):
        if before != after and field not in changed_fields:
            changed_fields.append(field)
    # 重新计算还缺哪些必填字段（destination/start_date/end_date）
    remaining_missing_fields = compute_missing_fields(next_state)

    return SessionMergeResult(
        next_state=next_state,
        remaining_missing_fields=remaining_missing_fields,
        changed_fields=changed_fields,
    )


def _normalize_date_range(state: SessionRequestState, request_patch: dict[str, Any] | None = None) -> SessionRequestState:
    """日期区间与天数的一致性归一化（幂等）"""
    patch = request_patch or {}
    patch_days = "days" in patch and patch.get("days") is not None
    # 方向修正（不依赖 days，改期残留的反向区间也能自愈）
    if state.start_date and state.end_date and state.end_date < state.start_date:
        state = state.model_copy(update={"start_date": state.end_date, "end_date": state.start_date})
    if state.days is None or state.days <= 0:
        return state
    days = state.days
    try:
        # 显式天数 + 区间完整：以最新给的日期为锚重算（patch 给了 end 用 end 锚，否则 start 锚）
        if patch_days and state.start_date and state.end_date:
            if "start_date" not in patch and "end_date" in patch:
                end = date.fromisoformat(state.end_date)
                start = end - timedelta(days=days - 1)
            else:
                start = date.fromisoformat(state.start_date)
                end = start + timedelta(days=days - 1)
            return state.model_copy(
                update={"start_date": start.isoformat(), "end_date": end.isoformat(), "days": None}
            )
        if state.start_date and not state.end_date:
            start = date.fromisoformat(state.start_date)
            end = start + timedelta(days=days - 1)
            return state.model_copy(update={"end_date": end.isoformat(), "days": None})
        if state.end_date and not state.start_date:
            end = date.fromisoformat(state.end_date)
            start = end - timedelta(days=days - 1)
            return state.model_copy(update={"start_date": start.isoformat(), "days": None})
    except ValueError:
        # 历史数据可能含非 ISO 日期，保持原样由下游防御
        return state
    # 区间完整且非显式 days：区间权威，清冗余 days
    if state.start_date and state.end_date:
        return state.model_copy(update={"days": None})
    return state
