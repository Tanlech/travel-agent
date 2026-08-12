from __future__ import annotations

import json
from typing import Any

from app.domain.session.schema import SessionState
from app.infrastructure.config.settings import settings
from app.infrastructure.redis_client import get_redis


def _state_key(session_id: str) -> str:
    return f"session:{session_id}:state"


def _artifacts_key(session_id: str) -> str:
    return f"session:{session_id}:artifacts"


class RedisSessionRepository:
    """SessionState 的 Redis 持久化实现。

    - session:{id}:state      存 SessionState 完整 JSON
    - session:{id}:artifacts  存 current_plan / current_draft 完整 payload（供 revise 取用）
    两者都带 TTL，避免无限堆积。
    """

    def __init__(self, redis_client: Any | None = None) -> None:
        self._redis = redis_client

    @property
    def redis(self):
        if self._redis is None:
            self._redis = get_redis()
        return self._redis

    def load(self, session_id: str) -> SessionState | None:
        if not session_id:
            return None
        raw = self.redis.get(_state_key(session_id))
        if not raw:
            return None
        return SessionState.model_validate_json(raw)

    def save(self, session_state: SessionState) -> None:
        payload = session_state.model_dump_json()
        self.redis.set(
            _state_key(session_state.session_id),
            payload,
            ex=settings.redis_ttl_seconds,
        )

    def load_artifacts(self, session_id: str) -> tuple[dict | None, dict | None]:
        if not session_id:
            return None, None
        raw = self.redis.get(_artifacts_key(session_id))
        if not raw:
            return None, None
        data = json.loads(raw)
        return data.get("plan"), data.get("draft")

    def save_artifacts(self, session_id: str, plan: dict | None, draft: dict | None) -> None:
        payload = json.dumps({"plan": plan, "draft": draft}, ensure_ascii=False)
        self.redis.set(_artifacts_key(session_id), payload, ex=settings.redis_ttl_seconds)


redis_session_repository = RedisSessionRepository()
