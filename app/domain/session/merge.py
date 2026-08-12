from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from app.domain.intent.schema import REQUIRED_PATCH_FIELDS
from app.domain.session.schema import SessionMergeResult, SessionRequestState


ALLOWED_PATCH_FIELDS = {
    "destination",
    "start_date",
    "end_date",
    "days",
    "departure_city",
    "travelers",
    "preferences",
    "must_visit_spots",
    "optional_spots",
    "avoid_spots",
}

LIST_FIELDS = {
    "preferences",
    "must_visit_spots",
    "optional_spots",
    "avoid_spots",
}

SCALAR_FIELDS = {
    "destination",
    "start_date",
    "end_date",
    "days",
    "departure_city",
    "travelers",
}

# 必填关键字段（有序：缺字段时的追问顺序 = 定义顺序）
# 与 intent 层 REQUIRED_PATCH_FIELDS 单一来源，避免两处维护漂移
REQUIRED_FIELDS = REQUIRED_PATCH_FIELDS


# 统一维护 patch -> full state 的合并规则
# 规则：
# 1. patch 没出现的字段不改
# 2. None 和空字符串不覆盖旧值
# 3. 标量字段直接覆盖
# 4. 列表字段第一版按整体替换处理
# 5. 只允许白名单字段参与 merge

def merge_request_state(
    current_state: SessionRequestState | None,
    request_patch: dict[str, Any],
) -> SessionMergeResult:
    # 以旧状态为基底（无旧状态时用空状态）
    previous_state = current_state or SessionRequestState()
    previous_dump = previous_state.model_dump()
    next_dump = dict(previous_dump)  # 在副本上累加，不改原对象

    applied_patch: dict[str, Any] = {}  # 本轮真正生效的 patch（过滤后）
    changed_fields: list[str] = []       # 本轮发生变化的字段名

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

        # 规则4：列表字段强制转 list；标量字段原样用
        # ⚠️ 可优化点：列表字段当前是"整体替换"语义，不是追加
        #    如用户先说"喜欢人文"再说"也喜欢自然"，会覆盖成只剩"自然"
        #    后续若要支持追加，需在此区分"替换 vs 追加"策略
        normalized_value = list(value) if field in LIST_FIELDS else value
        if next_dump.get(field) != normalized_value:
            next_dump[field] = normalized_value
            changed_fields.append(field)
        applied_patch[field] = normalized_value

    # 用合并后的 dict 重建强类型对象（会做字段校验）
    next_state = SessionRequestState(**next_dump)
    # 日期区间与天数的一致性归一化（如"8月10号玩3天"补全 end_date）
    next_state = _normalize_date_range(next_state)
    # 重新计算还缺哪些必填字段（destination/start_date/end_date）
    remaining_missing_fields = compute_missing_fields(next_state)

    return SessionMergeResult(
        previous_state=previous_state,
        applied_patch=applied_patch,
        next_state=next_state,
        changed_fields=changed_fields,
        remaining_missing_fields=remaining_missing_fields,
    )


def _normalize_date_range(state: SessionRequestState) -> SessionRequestState:
    """日期区间与天数的一致性归一化（幂等）。

    - start/end 齐全：冗余 days 一律清除（区间成为权威，杜绝"区间 3 天但 days=5"矛盾）；
      若 end 早于 start（改期时旧日期残留导致反向），对调修正
    - start + days → 补全 end = start + days - 1，并清除 days
    - end + days   → 补全 start = end - days + 1，并清除 days
    - 无 days 或无任何一端日期时不动（无法定位，交给追问）
    """
    # 方向防御不依赖 days：改期场景可能"新 start + 旧 end"残留出反向区间，
    # 不能等 days 分支才处理（无 days 时原本会直接返回）
    if state.start_date and state.end_date:
        if state.end_date < state.start_date:
            state = state.model_copy(update={"start_date": state.end_date, "end_date": state.start_date})
        if state.days is not None:
            return state.model_copy(update={"days": None})
        return state
    if not state.days or int(state.days) <= 0:
        return state
    days = int(state.days)
    try:
        if state.start_date and not state.end_date:
            start = date.fromisoformat(state.start_date)
            end = start + timedelta(days=days - 1)
            return state.model_copy(update={"end_date": end.isoformat(), "days": None})
        if state.end_date and not state.start_date:
            end = date.fromisoformat(state.end_date)
            start = end - timedelta(days=days - 1)
            return state.model_copy(update={"start_date": start.isoformat(), "days": None})
    except ValueError:
        # 历史数据里可能有非 ISO 日期（旧版本），保持原样，由下游防御
        return state
    return state


# 当前完整状态还缺哪些关键字段，由 session 层统一判断
# 字段集合与顺序来自 REQUIRED_FIELDS（意图识别层也复用同一常量，保证口径一致）

def compute_missing_fields(request_state: SessionRequestState) -> list[str]:
    missing: list[str] = []
    for field in REQUIRED_FIELDS:
        if not str(getattr(request_state, field, None) or "").strip():
            missing.append(field)
    return missing
