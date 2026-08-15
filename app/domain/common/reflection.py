"""跨层共享的反思结果模型（domain 容器持有，agents 层 re-export）"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ReflectionIssue(BaseModel):
    code: str
    message: str
    severity: Literal["warning", "error"]
    scope: str
    fix_hint: str | None = None


class ReflectionResult(BaseModel):
    status: Literal["accept", "revise"] = "accept"
    issues: list[ReflectionIssue] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    repair_scope: list[str] = Field(default_factory=list)


class ReflectionLLMResult(BaseModel):
    status: Literal["accept", "revise"] = "accept"
    issues: list[ReflectionIssue] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)
    repair_scope: list[str] = Field(default_factory=list)
