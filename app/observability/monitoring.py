from __future__ import annotations

import logging
from collections import defaultdict

_logger = logging.getLogger("app")


class StructuredLogger:
    """结构化日志封装：把 event + fields 转成一行标准日志。"""

    def _log(self, level: int, event: str, fields: dict) -> None:
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        if detail:
            _logger.log(level, "%s %s", event, detail)
        else:
            _logger.log(level, "%s", event)

    def info(self, event: str, **fields) -> None:
        self._log(logging.INFO, event, fields)

    def error(self, event: str, **fields) -> None:
        self._log(logging.ERROR, event, fields)


class MetricsRecorder:
    """最小计数器：按 (name, labels) 累计，后续可替换为真实指标后端。"""

    def __init__(self) -> None:
        self._counters: dict[tuple, int] = defaultdict(int)

    def record(self, name: str, value: int = 1, **labels) -> None:
        key = (name, tuple(sorted(labels.items())))
        self._counters[key] += value
        _logger.debug("metric %s value=%s labels=%s", name, value, labels)


app_logger = StructuredLogger()
metrics_recorder = MetricsRecorder()
