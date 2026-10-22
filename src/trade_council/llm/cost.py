"""Token and cost accounting for model calls."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import BudgetExceededError
from .base import CompletionResult
from .models import BudgetConfig, ModelSpec


@dataclass
class CostSnapshot:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float | None = 0.0
    unknown_pricing_calls: int = 0
    calls: int = 0


class CostLedger:
    def __init__(self) -> None:
        self.snapshot = CostSnapshot()

    def record(self, result: CompletionResult, spec: ModelSpec | None) -> CostSnapshot:
        self.snapshot.prompt_tokens += result.usage.prompt_tokens
        self.snapshot.completion_tokens += result.usage.completion_tokens
        self.snapshot.total_tokens += result.usage.total_tokens
        self.snapshot.calls += 1
        if spec and spec.input_price_per_mtok is not None and spec.output_price_per_mtok is not None:
            if self.snapshot.estimated_cost is None:
                return self.snapshot
            self.snapshot.estimated_cost += (
                result.usage.prompt_tokens / 1_000_000 * spec.input_price_per_mtok
                + result.usage.completion_tokens / 1_000_000 * spec.output_price_per_mtok
            )
        else:
            self.snapshot.unknown_pricing_calls += 1
            self.snapshot.estimated_cost = None
        return self.snapshot


def enforce_budget(snapshot: CostSnapshot, budget: BudgetConfig) -> None:
    if budget.max_run_cost is None or snapshot.estimated_cost is None:
        return
    if snapshot.estimated_cost <= budget.max_run_cost:
        return
    message = (
        f"Estimated run cost {snapshot.estimated_cost:.4f} {budget.currency} "
        f"exceeds max_run_cost {budget.max_run_cost:.4f}"
    )
    if budget.on_exceed == "abort":
        raise BudgetExceededError(message)
    # warn/degrade are represented by the caller; no exception for warn.
