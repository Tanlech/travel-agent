from __future__ import annotations

from app.agents.schema.planning import PlanningRequest
from app.domain.context.user import UserContext
from app.domain.memory.schema import TripMemory, UserMemory
from app.domain.memory.store import MemoryStore, memory_store

"""记忆融合：存储偏好 + 当前请求偏好 → UserContext；规划后持久化"""

# 否定前缀（长前缀优先，避免"不喜欢"被"不"提前截断）
_NEGATION_PREFIXES = ("不喜欢", "不想", "不要", "讨厌", "不愿意", "别", "不")


def _parse_preference_item(item: str) -> tuple[str | None, str]:
    """拆否定前缀，返回 (前缀, 正文)；无否定时前缀为 None"""
    for prefix in _NEGATION_PREFIXES:
        if item.startswith(prefix):
            body = item[len(prefix):].strip()
            if body:
                return prefix, body
    return None, item.strip()


class MemoryManager:
    """读取侧融合记忆与当前请求，写入侧持久化（store 可注入）"""

    def __init__(self, store: MemoryStore | None = None) -> None:
        self._store: MemoryStore = store or memory_store

    def build_user_context(self, request: PlanningRequest, *, user_id: str | None = None) -> UserContext:
        """融合 stored 记忆与当前请求偏好 → UserContext（负面偏好进 disliked_styles）"""
        stored = self._store.load_user_memory(user_id)
        # 拆否定：正向偏好做风格判定，"不要紧凑"不进正向信号
        positive_prefs: list[str] = []
        disliked_items: list[str] = []
        for item in (p.strip() for p in request.preferences if p and p.strip()):
            prefix, body = _parse_preference_item(item)
            if prefix:
                disliked_items.append(body)
            else:
                positive_prefs.append(item.strip())

        pace = stored.pace_preference if stored else None
        if any("轻松" in item for item in positive_prefs):
            pace = "relaxed"
        elif any("深度" in item or "紧凑" in item for item in positive_prefs):
            pace = "dense"

        accept_theme_park = stored.accept_theme_park if stored else None
        if any(any(keyword in item for keyword in ["乐园", "方特", "欢乐谷", "迪士尼"]) for item in positive_prefs):
            accept_theme_park = True
        accept_nightlife = stored.accept_nightlife if stored else None
        if any(any(keyword in item for keyword in ["夜游", "夜景", "演艺"]) for item in positive_prefs):
            accept_nightlife = True

        family_friendly = stored.family_friendly if stored else None
        if any(any(keyword in item.lower() for keyword in ("亲子", "儿童", "带娃", "带小孩", "family", "kids")) for item in positive_prefs):
            family_friendly = True
        senior_friendly = stored.senior_friendly if stored else None
        if any(any(keyword in item.lower() for keyword in ("老人", "长辈", "父母", "银发", "senior", "elder")) for item in positive_prefs):
            senior_friendly = True

        preferred_styles = list(dict.fromkeys((stored.preferred_styles if stored else []) + positive_prefs))
        disliked_styles = list(dict.fromkeys((stored.disliked_styles if stored else []) + disliked_items))
        # 否定信号补强：明确的"不要 X"写回布尔 False（正向 True 优先，不覆盖）
        for body in disliked_items:
            if accept_theme_park is not True and any(kw in body for kw in ("乐园", "方特", "欢乐谷", "迪士尼")):
                accept_theme_park = False
            if accept_nightlife is not True and any(kw in body for kw in ("夜游", "夜景", "演艺")):
                accept_nightlife = False
            if family_friendly is not True and any(kw in body for kw in ("亲子", "儿童", "带娃", "带小孩")):
                family_friendly = False
            if senior_friendly is not True and any(kw in body for kw in ("老人", "长辈", "父母", "银发")):
                senior_friendly = False
        return UserContext(
            preferred_styles=preferred_styles,
            disliked_styles=disliked_styles,
            pace_preference=pace,
            accept_theme_park=accept_theme_park,
            accept_nightlife=accept_nightlife,
            family_friendly=family_friendly,
            senior_friendly=senior_friendly,
        )

    def persist_user_memory(self, user_id: str | None, context: UserContext) -> None:
        """把本轮 UserContext 写回用户记忆（整体覆盖；字段与 UserMemory 一致，model_dump 直接映射）"""
        if not user_id:
            return
        self._store.save_user_memory(UserMemory(user_id=user_id, **context.model_dump()))

    def persist_trip_memory(self, user_id: str | None, request: PlanningRequest, accepted_spots: list[str], rejected_spots: list[str], summary: str | None, response_mode: str | None = None) -> None:
        """追加一条行程记忆（store 内部裁剪上限）"""
        if not user_id:
            return
        self._store.append_trip_memory(
            user_id,
            TripMemory(
                destination=request.destination,
                days=request.days,
                budget=request.budget,
                accepted_spots=accepted_spots,
                rejected_spots=rejected_spots,
                summary=summary,
                response_mode=response_mode,
            ),
        )


memory_manager = MemoryManager()
