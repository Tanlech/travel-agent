"""统一 UTC 时钟（各层取时间统一走这里，避免 datetime.now(timezone.utc) 分散调用）"""

from __future__ import annotations

from datetime import datetime, timezone


def utc_now() -> datetime:
    """当前 UTC 时间（aware datetime，供时间戳字段与审计使用）"""
    return datetime.now(timezone.utc)
