from __future__ import annotations

from app.domain.memory.schema import SessionMemory, TripMemory, UserMemory


class MemoryStore:
    def __init__(self) -> None:
        self._user_memory: dict[str, UserMemory] = {}
        self._session_memory: dict[str, SessionMemory] = {}
        self._trip_memory: dict[str, list[TripMemory]] = {}

    def load_user_memory(self, user_id: str | None) -> UserMemory | None:
        if not user_id:
            return None
        return self._user_memory.get(user_id)

    def save_user_memory(self, memory: UserMemory) -> None:
        if memory.user_id:
            self._user_memory[memory.user_id] = memory

    def load_session_memory(self, session_id: str | None) -> SessionMemory | None:
        if not session_id:
            return None
        return self._session_memory.get(session_id)

    def save_session_memory(self, memory: SessionMemory) -> None:
        if memory.session_id:
            self._session_memory[memory.session_id] = memory

    def append_trip_memory(self, user_id: str | None, memory: TripMemory) -> None:
        if not user_id:
            return
        self._trip_memory.setdefault(user_id, []).append(memory)

    def load_trip_memories(self, user_id: str | None) -> list[TripMemory]:
        if not user_id:
            return []
        return list(self._trip_memory.get(user_id, []))


memory_store = MemoryStore()
