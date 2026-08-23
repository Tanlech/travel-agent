"""记忆存取：协议 + 内存实现 + Redis 实现（键 mem:user:{id} / mem:trip:{id}）"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from app.domain.memory.schema import TripMemory, UserMemory
from app.infrastructure.settings import settings
from app.infrastructure.redis_client import get_redis

logger = logging.getLogger(__name__)

# 单个用户保留的行程记忆条数上限（超限裁剪最旧）
MAX_TRIP_MEMORIES = 10


class MemoryStore(Protocol):
    """记忆存取接口（可注入替换，测试用 InMemoryStore）"""

    def load_user_memory(self, user_id: str | None) -> UserMemory | None: ...
    def save_user_memory(self, memory: UserMemory) -> None: ...
    def append_trip_memory(self, user_id: str | None, memory: TripMemory) -> None: ...
    def load_trip_memories(self, user_id: str | None) -> list[TripMemory]: ...


def _user_key(user_id: str) -> str:
    return f"mem:user:{user_id}"


def _trip_key(user_id: str) -> str:
    return f"mem:trip:{user_id}"


class InMemoryStore:
    """进程内实现（测试/无 Redis 兜底，重启即丢）"""

    def __init__(self) -> None:
        self._user_memory: dict[str, UserMemory] = {}
        self._trip_memory: dict[str, list[TripMemory]] = {}

    def load_user_memory(self, user_id: str | None) -> UserMemory | None:
        """读取用户偏好（匿名返回 None）"""
        if not user_id:
            return None
        return self._user_memory.get(user_id)

    def save_user_memory(self, memory: UserMemory) -> None:
        """写用户偏好（整体覆盖）"""
        if memory.user_id:
            self._user_memory[memory.user_id] = memory

    def append_trip_memory(self, user_id: str | None, memory: TripMemory) -> None:
        """追加行程记忆并裁剪上限"""
        if not user_id:
            return
        bucket = self._trip_memory.setdefault(user_id, [])
        bucket.append(memory)
        del bucket[:-MAX_TRIP_MEMORIES]

    def load_trip_memories(self, user_id: str | None) -> list[TripMemory]:
        """读取行程记忆列表（匿名返回空）"""
        if not user_id:
            return []
        return list(self._trip_memory.get(user_id, []))


class RedisMemoryStore:
    """Redis 实现（懒加载连接，行为与 InMemoryStore 一致）"""

    def __init__(self, redis_client: Any | None = None) -> None:
        self._redis = redis_client

    @property
    def redis(self):
        if self._redis is None:
            self._redis = get_redis()
        return self._redis

    def load_user_memory(self, user_id: str | None) -> UserMemory | None:
        """读取用户偏好并续期 TTL（坏数据返回 None）"""
        if not user_id:
            return None
        key = _user_key(user_id)
        raw = self.redis.get(key)
        if not raw:
            return None
        self.redis.expire(key, settings.redis_ttl_seconds)
        try:
            return UserMemory.model_validate_json(raw)
        except Exception:
            # 用户偏好数据损坏（半写/版本升级遗留）时按无记忆处理，并记录便于排查
            logger.warning("user memory 数据损坏，按无记忆处理: %s", user_id)
            return None

    def save_user_memory(self, memory: UserMemory) -> None:
        """写用户偏好（JSON + TTL）"""
        if not memory.user_id:
            return
        self.redis.set(_user_key(memory.user_id), memory.model_dump_json(), ex=settings.redis_ttl_seconds)

    def append_trip_memory(self, user_id: str | None, memory: TripMemory) -> None:
        """RPUSH 追加 + LTRIM 保留最近 MAX_TRIP_MEMORIES 条 + TTL"""
        if not user_id:
            return
        pipe = self.redis.pipeline()
        pipe.rpush(_trip_key(user_id), memory.model_dump_json())
        pipe.ltrim(_trip_key(user_id), -MAX_TRIP_MEMORIES, -1)
        pipe.expire(_trip_key(user_id), settings.redis_ttl_seconds)
        pipe.execute()

    def load_trip_memories(self, user_id: str | None) -> list[TripMemory]:
        """读取行程记忆列表（坏数据跳过；读时续期 TTL，与 user memory 口径一致）"""
        if not user_id:
            return []
        key = _trip_key(user_id)
        memories: list[TripMemory] = []
        for raw in self.redis.lrange(key, 0, -1):
            try:
                memories.append(TripMemory.model_validate_json(raw))
            except Exception:
                logger.warning("trip memory 数据损坏，跳过该条: %s", user_id)
                continue
        if memories:
            # 读到内容即续期：否则用户偏好因每次读取续期长期存活、行程记忆却按最后写入时间过期，
            # 活跃用户隔段时间回来 load_trip_history 会静默变空（intent/revise 历史参考失效）
            self.redis.expire(key, settings.redis_ttl_seconds)
        return memories


# 默认走 Redis（与 SessionState 持久化对齐）；测试可用 InMemoryStore 注入 MemoryManager
memory_store = RedisMemoryStore()
