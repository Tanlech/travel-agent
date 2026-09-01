from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Protocol

import redis

from app.agent.domain.session.schema import SessionState
from app.infrastructure.settings import settings
from app.infrastructure.redis_client import get_redis

logger = logging.getLogger(__name__)


class SessionRepository(Protocol):
    """会话读写接口（含 artifacts，乐观并发：版本不一致返回 None，由调用方重新加载重试）"""

    def load(self, session_id: str) -> SessionState | None: ...
    def save(self, session_state: SessionState) -> SessionState | None: ...
    def load_artifacts(self, session_id: str) -> tuple[dict | None, dict | None]: ...
    def save_with_artifacts(self, session_state: SessionState, plan: dict | None, draft: dict | None) -> SessionState | None: ...
    def load_idempotent_response(self, session_id: str, request_id: str) -> dict | None: ...
    def save_idempotent_response(self, session_id: str, request_id: str, payload: dict) -> None: ...
    def list_summaries(self, user_id: str | None = None) -> list[dict]: ...
    def delete_by_user(self, user_id: str) -> int: ...
    def delete(self, session_id: str) -> None: ...
    def append_event(self, session_id: str, event_type: str, data: dict | None = None) -> None: ...
    def list_events(self, session_id: str, limit: int = 200) -> list[dict]: ...
    def push_undo(self, session_id: str, snapshot: dict | None) -> None: ...
    def pop_undo(self, session_id: str) -> dict | None: ...
    def has_undo(self, session_id: str) -> bool: ...
    def pop_undo_if_any(self, session_id: str) -> tuple[bool, dict | None]: ...
    def restore_artifacts(self, session_id: str, snapshot: dict | None) -> None: ...


def _state_key(session_id: str) -> str:
    if not session_id:
        raise ValueError("session_id must not be empty")
    return f"session:{session_id}:state"


def _artifacts_key(session_id: str) -> str:
    if not session_id:
        raise ValueError("session_id must not be empty")
    return f"session:{session_id}:artifacts"


def _idempotent_key(session_id: str, request_id: str) -> str:
    return f"session:{session_id}:req:{request_id}"


def _events_key(session_id: str) -> str:
    return f"session:{session_id}:events"


def _undo_key(session_id: str) -> str:
    return f"session:{session_id}:undo"


# 事件日志与撤销栈条数上限（只追加，取最新窗口）
_EVENT_LOG_LIMIT = 500
_UNDO_STACK_LIMIT = 8

# 原子"判断并弹栈"：列表为空返回 false，否则 RPOP 出最近快照
_POP_UNDO_IF_ANY_LUA = """
local key = KEYS[1]
if redis.call('LLEN', key) == 0 then
    return false
end
return redis.call('RPOP', key)
"""


# 请求级幂等缓存 TTL：仅对"短时间内同 request_id 重试"有效，短窗口即够；
# 若与会话同 TTL（24h）会长期保留完整 plan/draft 副本，纯属存储浪费
_IDEMPOTENT_RESPONSE_TTL_SECONDS = 300


def _extract_version(raw: str | None) -> int:
    """从 state 原始 JSON 读乐观版本号，空/损坏按 0（首次写入）"""
    if not raw:
        return 0
    try:
        return int(json.loads(raw).get("version", 0))
    except (ValueError, TypeError):
        return 0


class RedisSessionRepository:
    """SessionState 的 Redis 持久化实现
    session:{id}:state 存完整 JSON（含版本号）；session:{id}:artifacts 存产物 payload；
    两者带 TTL，读 state 时顺带续期
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

    def save(self, session_state: SessionState) -> SessionState | None:
        """乐观保存：版本一致才写入并 +1；冲突返回 None（调用方重新 load 后重试）"""
        return self._optimistic_save(session_state, artifacts_payload=None)

    def save_with_artifacts(self, session_state: SessionState, plan: dict | None, draft: dict | None) -> SessionState | None:
        """乐观保存 + 产物原子提交：state 与 artifacts 同事务写入，冲突整体回滚（防摘要错位）"""
        return self._optimistic_save(
            session_state,
            artifacts_payload=json.dumps({"plan": plan, "draft": draft}, ensure_ascii=False),
        )

    def _optimistic_save(self, session_state: SessionState, artifacts_payload: str | None, max_attempts: int = 5) -> SessionState | None:
        if not session_state.session_id:
            return None
        key = _state_key(session_state.session_id)
        pipe = self.redis.pipeline()
        for _ in range(max_attempts):
            try:
                pipe.watch(key)
                if _extract_version(pipe.get(key)) != session_state.version:
                    pipe.unwatch()
                    return None
                # 版本 +1 后写入（后续 save 才能正确比较）
                bumped = session_state.model_copy(update={"version": session_state.version + 1})
                pipe.multi()
                pipe.set(key, bumped.model_dump_json(), ex=settings.redis_ttl_seconds)
                if artifacts_payload is not None:
                    pipe.set(_artifacts_key(session_state.session_id), artifacts_payload, ex=settings.redis_ttl_seconds)
                pipe.execute()
                return bumped
            except redis.WatchError:
                # WATCH 期间 key 被其他客户端修改，重置后重放
                pipe.reset()
                continue
        # 持续竞争时放弃本次写入，避免无限循环；返回 None 由调用方重试
        return None

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

    def load_idempotent_response(self, session_id: str, request_id: str) -> dict | None:
        """请求级幂等：读取该 request_id 首次处理的结果 payload；无/损坏返回 None"""
        if not session_id or not request_id:
            return None
        raw = self.redis.get(_idempotent_key(session_id, request_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            logger.warning("idempotent response 数据损坏，按未缓存处理: %s/%s", session_id, request_id)
            return None

    def save_idempotent_response(self, session_id: str, request_id: str, payload: dict) -> None:
        """请求级幂等：缓存某 request_id 的处理结果（短窗口 TTL，防重试重复执行副作用）"""
        if not session_id or not request_id:
            return
        self.redis.set(
            _idempotent_key(session_id, request_id),
            json.dumps(payload, ensure_ascii=False),
            ex=_IDEMPOTENT_RESPONSE_TTL_SECONDS,
        )

    # ===================== 会话事件日志（只追加，历史重建基础） =====================

    def append_event(self, session_id: str, event_type: str, data: dict | None = None) -> None:
        """向会话追加一条事件（user_message / assistant_reply / plan_commit ...）。
        只追加：不修改历史，服务端可据此重建完整上下文。"""
        if not session_id:
            return
        event = {
            "type": event_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        key = _events_key(session_id)
        self.redis.rpush(key, json.dumps(event, ensure_ascii=False))
        self.redis.ltrim(key, -_EVENT_LOG_LIMIT, -1)
        self.redis.expire(key, settings.redis_ttl_seconds)

    def list_events(self, session_id: str, limit: int = 200) -> list[dict]:
        """按时间顺序返回事件日志（取最近 limit 条）；损坏条目跳过"""
        if not session_id:
            return []
        raw_items = self.redis.lrange(_events_key(session_id), -limit, -1)
        events: list[dict] = []
        for raw in raw_items:
            try:
                events.append(json.loads(raw))
            except (ValueError, TypeError):
                continue
        return events

    # ===================== 撤销栈（plan/draft 快照，LIFO） =====================

    def push_undo(self, session_id: str, snapshot: dict | None) -> None:
        """入栈一份要覆盖前的快照 {plan, draft}；超过上限截掉最早的"""
        if not session_id or snapshot is None:
            return
        key = _undo_key(session_id)
        self.redis.rpush(key, json.dumps(snapshot, ensure_ascii=False))
        self.redis.ltrim(key, -_UNDO_STACK_LIMIT, -1)
        self.redis.expire(key, settings.redis_ttl_seconds)

    def pop_undo(self, session_id: str) -> dict | None:
        """弹出最近的快照用于撤销；无则返回 None"""
        if not session_id:
            return None
        raw = self.redis.rpop(_undo_key(session_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None

    def has_undo(self, session_id: str) -> bool:
        if not session_id:
            return False
        return bool(self.redis.llen(_undo_key(session_id)))

    def pop_undo_if_any(self, session_id: str) -> tuple[bool, dict | None]:
        """原子地判断并弹出最近的撤销快照（避免 has_undo 与 rpop 两步间的竞争）：
        返回 (是否有可撤销, 快照)；无则 (False, None)。"""
        if not session_id:
            return False, None
        raw = self.redis.eval(_POP_UNDO_IF_ANY_LUA, 1, _undo_key(session_id))
        if not raw:
            return False, None
        try:
            return True, json.loads(raw)
        except (ValueError, TypeError):
            return True, None

    def restore_artifacts(self, session_id: str, snapshot: dict | None) -> None:
        """把快照写回 artifacts（撤销恢复用）；无快照则清空产物"""
        if not session_id:
            return
        if snapshot is None:
            snapshot = {"plan": None, "draft": None}
        self.redis.set(
            _artifacts_key(session_id),
            json.dumps({"plan": snapshot.get("plan"), "draft": snapshot.get("draft")}, ensure_ascii=False),
            ex=settings.redis_ttl_seconds,
        )
        return

    def list_summaries(self, user_id: str | None = None) -> list[dict]:
        """扫描所有会话，返回列表摘要（供前端多会话列表服务端同步 / 后台按用户过滤）。"""
        sessions: list[dict] = []
        try:
            keys = list(self.redis.scan_iter("session:*:state", count=100))
        except Exception as exc:  # Redis 不可用时不阻塞会话列表接口
            logger.warning("list_summaries scan 失败: %s", exc)
            return sessions
        for key in keys:
            try:
                # decode_responses=True 下 scan_iter 已返回 str；兼容旧 bytes 连接
                raw_key = key.decode(errors="ignore") if isinstance(key, bytes) else key
                sid = raw_key.removeprefix("session:").removesuffix(":state")
            except Exception:
                continue
            if not sid or sid.find(":") != -1:
                # 会话 id 理论上不含冒号；跳过异常 key，避免把 idempotent/其他 key 误当会话
                continue
            raw = self.redis.get(key)
            if not raw:
                continue
            try:
                state = SessionState.model_validate_json(raw)
            except ValueError:
                logger.warning("list_summaries 会话数据损坏，跳过: %s", sid)
                continue
            if user_id and state.user_id != user_id:
                continue
            title = state.recent_messages[0].content if state.recent_messages else ""
            sessions.append(
                {
                    "session_id": sid,
                    "user_id": state.user_id,
                    "title": title[:40] if isinstance(title, str) else "",
                    "stage": state.conversation_stage,
                    "revision_count": state.revision_count,
                    "message_count": len(state.recent_messages),
                    "has_plan": state.artifacts.has_plan,
                    "updated_at": state.updated_at.isoformat() if state.updated_at else None,
                }
            )
        return sessions

    def delete_by_user(self, user_id: str) -> int:
        """删除某用户的全部会话（state/artifacts/幂等缓存），返回删除数。"""
        if not user_id:
            return 0
        try:
            keys = list(self.redis.scan_iter("session:*:state", count=100))
        except Exception as exc:
            logger.warning("delete_by_user scan 失败: %s", exc)
            return 0
        deleted = 0
        for key in keys:
            try:
                raw_key = key.decode(errors="ignore") if isinstance(key, bytes) else key
                sid = raw_key.removeprefix("session:").removesuffix(":state")
                raw = self.redis.get(key)
                if not raw:
                    continue
                state = SessionState.model_validate_json(raw)
            except Exception:
                continue
            if state.user_id != user_id:
                continue
            self.delete(sid)
            deleted += 1
        return deleted

    def delete(self, session_id: str) -> None:
        """删除会话的 state / artifacts / 全部 idempotent 缓存。"""
        if not session_id:
            return
        keys = [_state_key(session_id), _artifacts_key(session_id), _events_key(session_id), _undo_key(session_id)]
        # 幂等缓存是 `session:{id}:req:*`，需前缀扫描删除
        try:
            keys.extend(list(self.redis.scan_iter(_idempotent_key(session_id, "*"), count=50)))
        except Exception as exc:
            logger.warning("delete 扫描幂等 key 失败: %s", exc)
        if keys:
            try:
                self.redis.delete(*keys)
            except Exception as exc:
                logger.warning("delete 失败: %s", exc)


redis_session_repository = RedisSessionRepository()
