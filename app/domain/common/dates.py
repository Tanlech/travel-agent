"""日期归一化工具（intent / session 层共享，统一合法性 + 跨年口径）。"""

from __future__ import annotations

import re
from datetime import date, datetime

# 无年份日期"明显过期"的容忍阈值（天）：早于今天超过该天数视为"上一年"，进位次年
_PAST_DATE_TOLERANCE_DAYS = 60


def normalize_date(value: str) -> str | None:
    """日期归一为 YYYY-MM-DD
    校验真实日期（如 2026-02-30 返回 None），杜绝非法日期污染下游
    无年份的月/日默认当年；明显过期（早于今天 _PAST_DATE_TOLERANCE_DAYS 天以上，如跨年）按次年
    """
    text = str(value).strip()
    if not text:
        return None
    m = re.fullmatch(r"(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})", text)
    if m:
        return _norm_ymd(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})月(\d{1,2})(?:号|日)", text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = datetime.now().year
        candidate = _norm_ymd(year, month, day)
        if candidate is None:
            return None
        if (date.fromisoformat(candidate) - date.today()).days < -_PAST_DATE_TOLERANCE_DAYS:
            candidate = _norm_ymd(year + 1, month, day)
        return candidate
    return None


def _norm_ymd(year: int, month: int, day: int) -> str | None:
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return None
