from __future__ import annotations

from app.domain.context.session import SessionContext
from app.domain.context.user import UserContext
from app.agents.schema.planning import PlanningRequest
from app.domain.memory.schema import SessionMemory, TripMemory, UserMemory
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

        traveler_text = " ".join(request.travelers)
        family_friendly = stored.family_friendly if stored else None
        if any(keyword in traveler_text.lower() for keyword in ["child", "kids", "family", "亲子", "儿童"]):
            family_friendly = True
        senior_friendly = stored.senior_friendly if stored else None
        if any(keyword in traveler_text.lower() for keyword in ["senior", "elder", "老人"]):
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

    def build_session_context(self, request: PlanningRequest, *, session_id: str | None = None) -> SessionContext:
        stored = memory_store.load_session_memory(session_id)
        confirmed_fields = list(stored.confirmed_fields) if stored else []
        if request.destination and "destination" not in confirmed_fields:
            confirmed_fields.append("destination")
        if request.days and "days" not in confirmed_fields:
            confirmed_fields.append("days")
        if request.budget is not None and "budget" not in confirmed_fields:
            confirmed_fields.append("budget")
        return SessionContext(
            session_id=session_id,
            confirmed_fields=confirmed_fields,
            pending_questions=list(stored.pending_questions) if stored else [],
            conversation_stage=stored.conversation_stage if stored else "new_plan",
            last_destination=stored.last_destination if stored else request.destination,
            revision_count=stored.revision_count if stored else 0,
        )

    def load_session_artifacts(self, session_id: str | None) -> tuple[dict | None, dict | None]:
        stored = memory_store.load_session_memory(session_id)
        if not stored:
            return None, None
        return stored.current_plan, stored.current_draft

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

    def persist_session_memory(self, context: SessionContext, *, current_plan: dict | None = None, current_draft: dict | None = None) -> None:
        if not context.session_id:
            return
        previous = memory_store.load_session_memory(context.session_id)
        memory_store.save_session_memory(
            SessionMemory(
                session_id=context.session_id,
                confirmed_fields=context.confirmed_fields,
                pending_questions=context.pending_questions,
                conversation_stage=context.conversation_stage,
                last_destination=context.last_destination,
                revision_count=context.revision_count,
                current_plan=current_plan if current_plan is not None else (previous.current_plan if previous else None),
                current_draft=current_draft if current_draft is not None else (previous.current_draft if previous else None),
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
