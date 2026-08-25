from __future__ import annotations

from app.agent.tools.schema.transport import TransportResult


def pick_transport_mode(result: TransportResult) -> dict | None:
    """从 transport tool 总结结果派生交通建议：取耗时最短的真实方案，输出模式/时长/距离/费用摘要。

    planning 与 revise 共用；transport tool 输出已是总结级（不走 AMap 原始 step 详情），
    因此注入产物的交通证据保持精简，避免大段路线信息污染 plan/LLM 上下文。"""
    candidates = []
    if result.taxi and result.taxi.duration_minutes:
        candidates.append((result.taxi.duration_minutes, "打车", result.taxi.distance_meters, result.taxi.cost))
    if result.transit and result.transit.duration_minutes:
        candidates.append((result.transit.duration_minutes, "公交/地铁", result.transit.distance_meters, result.transit.cost))
    if result.walk and result.walk.duration_minutes:
        candidates.append((result.walk.duration_minutes, "步行", result.walk.distance_meters, None))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    duration, mode, distance_meters, cost = candidates[0]
    return {
        "recommended_mode": mode,
        "duration_minutes": duration,
        "distance_km": round(distance_meters / 1000, 1) if distance_meters else None,
        "cost": cost,
    }
