from __future__ import annotations

from app.agents.schema.orchestrator import TokenBudgetPolicy


class TokenBudgetTracker:
    def __init__(self, policy: TokenBudgetPolicy) -> None:
        self.policy = policy
        self.spent = 0

    def allocate(self, amount: int) -> int:
        return max(0, min(amount, self.remaining()))

    def consume(self, amount: int) -> None:
        self.spent += max(0, amount)

    def remaining(self) -> int:
        return max(0, self.policy.total_budget - self.spent)
