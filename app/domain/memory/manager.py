from __future__ import annotations

from app.domain.context.user import UserContext
from app.agents.schema.planning import PlanningRequest
from app.domain.memory.schema import TripMemory, UserMemory
from app.domain.memory.store import memory_store


class MemoryManager:
    def build_user_context(self, request: PlanningRequest, *, user_id: str | None = None) -> UserContext:
        stored = memory_store.load_user_memory(user_id)
        preferences = [item.strip() for item in request.preferences if item and item.strip()]
        pace = stored.pace_preference if stored else None
        if any("轻松" in item for item in preferences):
            pace = "relaxed"
        elif any("深度" in item or "紧凑" in item for item in preferences):
            pace = "dense"

        accept_theme_park = stored.accept_theme_park if stored else None
        if any(any(keyword in item for keyword in ["乐园", "方特", "欢乐谷", "迪士尼"]) for item in preferences):
            accept_theme_park = True
        accept_nightlife = stored.accept_nightlife if stored else None
        if any(any(keyword in item for keyword in ["夜游", "夜景", "演艺"]) for item in preferences):
            accept_nightlife = True

        family_friendly = stored.family_friendly if stored else None
        if any(any(keyword in item.lower() for keyword in ("亲子", "儿童", "带娃", "带小孩", "family", "kids")) for item in preferences):
            family_friendly = True
        senior_friendly = stored.senior_friendly if stored else None
        if any(any(keyword in item.lower() for keyword in ("老人", "长辈", "父母", "银发", "senior", "elder")) for item in preferences):
            senior_friendly = True

        preferred_styles = list(dict.fromkeys((stored.preferred_styles if stored else []) + preferences))
        disliked_styles = list(stored.disliked_styles) if stored else []
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
        if not user_id:
            return
        memory_store.save_user_memory(
            UserMemory(
                user_id=user_id,
                preferred_styles=context.preferred_styles,
                disliked_styles=context.disliked_styles,
                accept_theme_park=context.accept_theme_park,
                accept_nightlife=context.accept_nightlife,
                pace_preference=context.pace_preference,
                family_friendly=context.family_friendly,
                senior_friendly=context.senior_friendly,
            )
        )

    def persist_trip_memory(self, user_id: str | None, request: PlanningRequest, accepted_spots: list[str], rejected_spots: list[str], summary: str | None, feedback: str | None = None) -> None:
        if not user_id:
            return
        memory_store.append_trip_memory(
            user_id,
            TripMemory(
                destination=request.destination,
                days=request.days,
                budget=request.budget,
                accepted_spots=accepted_spots,
                rejected_spots=rejected_spots,
                summary=summary,
                feedback=feedback,
            ),
        )


memory_manager = MemoryManager()
