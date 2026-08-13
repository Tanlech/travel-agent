from __future__ import annotations

from app.domain.memory.schema import TripMemory, UserMemory


class MemoryStore:
    """用户与行程记忆的内存实现。

    注意：会话状态（SessionState）已由 Redis 持久化（app.domain.session.repository），
    这里不再维护会话记忆，避免与新会话层双写分叉。
    """

    def __init__(self) -> None:
        self._user_memory: dict[str, UserMemory] = {}
        self._trip_memory: dict[str, list[TripMemory]] = {}

    def load_user_memory(self, user_id: str | None) -> UserMemory | None:
        if not user_id:
            return None
        return self._user_memory.get(user_id)

    def save_user_memory(self, memory: UserMemory) -> None:
        if memory.user_id:
            self._user_memory[memory.user_id] = memory

    def append_trip_memory(self, user_id: str | None, memory: TripMemory) -> None:
        if not user_id:
            return
        self._trip_memory.setdefault(user_id, []).append(memory)

    def load_trip_memories(self, user_id: str | None) -> list[TripMemory]:
        if not user_id:
            return []
        return list(self._trip_memory.get(user_id, []))


memory_store = MemoryStore()
