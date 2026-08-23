"""数值/字符串安全转换与网络退避重试工具：各 API/工具客户端统一复用"""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

_T = TypeVar("_T")


def retry_call(fn: Callable[[], _T], *, attempts: int = 3, backoff: float = 0.3) -> _T:
    """调用 fn()，瞬时异常退避重试，重试耗尽仍失败才抛出"""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception:
            if attempt < attempts - 1:
                time.sleep(backoff * (attempt + 1))
                continue
            raise


def safe_float(value: Any) -> float | None:
    """尽力转 float，失败/空值返回 None"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> int | None:
    """尽力转 int（容忍 '4.0' 这类小数串），失败/空值返回 None"""
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def safe_str(value: Any) -> str | None:
    """空值（None/空串/空容器/纯空白）转 None，否则转去除两侧空白的 str"""
    if value in (None, "", [], {}):
        return None
    return str(value).strip() or None