from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import redis

from app.domain.session.schema import SessionState
from app.infrastructure.config.settings import settings
from app.infrastructure.redis_client import get_redis

logger = logging.getLogger(__name__)


class SessionRepository(Protocol):
    """会话读写接口（含 artifacts，乐观并发：版本不一致返回 False，由调用方重新加载重试）"""

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
    """从 state 原始 JSON 读乐观版本号，空/损坏按 0（首次写入）"""
    if not raw:
        return 0
    try:
        return int(json.loads(raw).get("version", 0))
    except (ValueError, TypeError):
        return 0


class RedisSessionRepository:
    """SessionState 的 Redis 持久化实现。

    session:{id}:state 存完整 JSON（含版本号）；session:{id}:artifacts 存产物 payload；
    两者带 TTL，读 state 时顺带续期（活跃会话不失效）
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
        # 读取即续期 TTL（覆盖只读场景）
        self.redis.expire(key, settings.redis_ttl_seconds)
        try:
            state = SessionState.model_validate_json(raw)
        except ValueError:
            # 数据损坏（半写/版本升级遗留）时视为新会话，避免整条会话 500
            logger.warning("session state 数据损坏，按新会话处理: %s", session_id)
            return None
        # 产物与 state 同步续期，避免"state 存活、artifacts 已过期"导致 revise 取不到旧行程
        if state.artifacts.has_plan:
            self.redis.expire(_artifacts_key(session_id), settings.redis_ttl_seconds)
        return state

    def save(self, session_state: SessionState) -> bool:
        """乐观保存：版本一致才写入并 +1；冲突返回 False（调用方重新 load 后重试）"""
        return self._optimistic_save(session_state, artifacts_payload=None)

    def save_with_artifacts(self, session_state: SessionState, plan: dict | None, draft: dict | None) -> bool:
        """乐观保存 + 产物原子提交：state 与 artifacts 同事务写入，冲突整体回滚（防摘要错位）"""
        return self._optimistic_save(
            session_state,
            artifacts_payload=json.dumps({"plan": plan, "draft": draft}, ensure_ascii=False),
        )

    def _optimistic_save(self, session_state: SessionState, artifacts_payload: str | None, max_attempts: int = 5) -> bool:
        if not session_state.session_id:
            return False
        key = _state_key(session_state.session_id)
        pipe = self.redis.pipeline()
        for _ in range(max_attempts):
            try:
                pipe.watch(key)
                if _extract_version(pipe.get(key)) != session_state.version:
                    pipe.unwatch()
                    return False
                # 版本 +1 后写入（后续 save 才能正确比较）
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
        # 持续竞争时放弃本次写入，避免无限循环；返回 False 由调用方重试
        return False

    def load_artifacts(self, session_id: str) -> tuple[dict | None, dict | None]:
        if not session_id:
            return None, None
        raw = self.redis.get(_artifacts_key(session_id))
        if not raw:
            return None, None
        try:
            data = json.loads(raw)
        except ValueError:
            # 产物 JSON 损坏时按无产物处理，revise 分支会提示无行程
            logger.warning("session artifacts 数据损坏，按无产物处理: %s", session_id)
            return None, None
        return data.get("plan"), data.get("draft")


redis_session_repository = RedisSessionRepository()
