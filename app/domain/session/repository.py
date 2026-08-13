from __future__ import annotations

import json
from typing import Any, Protocol

import redis

from app.domain.session.schema import SessionState
from app.infrastructure.config.settings import settings
from app.infrastructure.redis_client import get_redis


class SessionRepository(Protocol):
    """会话读写接口（含 artifacts）。

    save / save_with_artifacts 采用乐观并发：版本不一致时拒绝写入并返回 False，
    调用方需要重新加载后重试，避免同一 session 的并发请求互相覆盖状态。
    save_with_artifacts 把 state 与产物放入同一事务提交，冲突时整体回滚，
    保证"新 plan + 新摘要"永远成对出现，不会出现产物已更新而摘要仍是旧的错位。
    """

    def load(self, session_id: str) -> SessionState | None: ...
    def save(self, session_state: SessionState) -> bool: ...
    def load_artifacts(self, session_id: str) -> tuple[dict | None, dict | None]: ...
    def save_with_artifacts(self, session_state: SessionState, plan: dict | None, draft: dict | None) -> bool: ...


def _state_key(session_id: str) -> str:
    if not session_id:
        raise ValueError("session_id must not be empty")
    return f"session:{session_id}:state"


def _artifacts_key(session_id: str) -> str:
    if not session_id:
        raise ValueError("session_id must not be empty")
    return f"session:{session_id}:artifacts"


def _extract_version(raw: str | None) -> int:
    """从 state 原始 JSON 里读取乐观版本号；空/损坏数据按 0 处理（首次写入）。"""
    if not raw:
        return 0
    try:
        return int(json.loads(raw).get("version", 0))
    except (ValueError, TypeError):
        return 0


class RedisSessionRepository:
    """SessionState 的 Redis 持久化实现。

    - session:{id}:state      存 SessionState 完整 JSON（含乐观版本号）
    - session:{id}:artifacts  存 current_plan / current_draft 完整 payload（供 revise 取用）
    两者都带 TTL，避免无限堆积；读 state 时顺带续期，活跃会话不会到期。
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
        key = _state_key(session_id)
        raw = self.redis.get(key)
        if not raw:
            return None
        # 活跃会话随读取续期 TTL（覆盖只读场景，如 GET /session）
        self.redis.expire(key, settings.redis_ttl_seconds)
        return SessionState.model_validate_json(raw)

    def save(self, session_state: SessionState) -> bool:
        """乐观保存：仅当 Redis 中当前版本与会话版本一致时写入，成功后版本号 +1。

        冲突（返回 False）说明期间有其他请求已推进该会话，调用方应重新 load 后重试，
        而不是直接用旧状态覆盖。
        """
        return self._optimistic_save(session_state, artifacts_payload=None)

    def save_with_artifacts(self, session_state: SessionState, plan: dict | None, draft: dict | None) -> bool:
        """乐观保存 + 产物原子提交：state 与 artifacts 在同一事务写入。

        冲突时整体回滚（state 与 artifacts 都不会落库），避免并发下出现
        "artifacts 是新 plan、摘要却还是旧 plan" 的错位状态。
        """
        return self._optimistic_save(
            session_state,
            artifacts_payload=json.dumps({"plan": plan, "draft": draft}, ensure_ascii=False),
        )

    def _optimistic_save(self, session_state: SessionState, artifacts_payload: str | None) -> bool:
        if not session_state.session_id:
            return False
        key = _state_key(session_state.session_id)
        pipe = self.redis.pipeline()
        while True:
            try:
                pipe.watch(key)
                if _extract_version(pipe.get(key)) != session_state.version:
                    pipe.unwatch()
                    return False
                # 递增版本号后写入：下一次 load 会拿到新版本，确保后续 save 能正确比较
                bumped = session_state.model_copy(update={"version": session_state.version + 1})
                pipe.multi()
                pipe.set(key, bumped.model_dump_json(), ex=settings.redis_ttl_seconds)
                if artifacts_payload is not None:
                    pipe.set(_artifacts_key(session_state.session_id), artifacts_payload, ex=settings.redis_ttl_seconds)
                pipe.execute()
                return True
            except redis.WatchError:
                # WATCH 期间 key 被其他客户端修改，重置后重放
                pipe.reset()
                continue

    def load_artifacts(self, session_id: str) -> tuple[dict | None, dict | None]:
        if not session_id:
            return None, None
        raw = self.redis.get(_artifacts_key(session_id))
        if not raw:
            return None, None
        data = json.loads(raw)
        return data.get("plan"), data.get("draft")


redis_session_repository = RedisSessionRepository()
