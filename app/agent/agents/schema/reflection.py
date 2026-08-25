"""兼容层：反思结果模型统一在 domain.common.reflection 定义，此处 re-export"""

from __future__ import annotations

from app.agent.domain.common.reflection import ReflectionIssue, ReflectionLLMResult, ReflectionResult

__all__ = ["ReflectionIssue", "ReflectionLLMResult", "ReflectionResult"]
