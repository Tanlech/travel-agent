from __future__ import annotations

import redis

from app.infrastructure.settings import settings


_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    """全局 Redis 连接单例

    优先使用 settings.redis_url，未配置时回退到本地 localhost:6379/0
    decode_responses=True 让返回值直接是 str，方便 JSON 序列化
    """
    global _redis
    if _redis is None:
        url = settings.redis_url or "redis://localhost:6379/0"
        _redis = redis.Redis.from_url(url, decode_responses=True)
    return _redis
