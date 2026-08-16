from __future__ import annotations

from pydantic import BaseModel


class TokenBudgetPolicy(BaseModel):
    """token 预算配置：total_budget 为单次请求的总预算上限"""

    total_budget: int = 12000


def default_budget_policy() -> TokenBudgetPolicy:
    return TokenBudgetPolicy()


class TokenBudgetTracker:
    """token 预算跟踪器：当前仅做额度钳制，未接入实际消耗统计"""

    def __init__(self, policy: TokenBudgetPolicy) -> None:
        self.policy = policy
        self.spent = 0

    def allocate(self, amount: int) -> int:
        return max(0, min(amount, self.remaining()))

    def consume(self, amount: int) -> None:
        self.spent += max(0, amount)

    def remaining(self) -> int:
        return max(0, self.policy.total_budget - self.spent)
