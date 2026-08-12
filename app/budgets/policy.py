from __future__ import annotations

from app.agents.schema.orchestrator import TokenBudgetPolicy


def default_budget_policy() -> TokenBudgetPolicy:
    return TokenBudgetPolicy()
