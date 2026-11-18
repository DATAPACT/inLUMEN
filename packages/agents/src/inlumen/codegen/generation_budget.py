"""Request-scoped limits shared by generation and all nested repair calls."""
import asyncio
import functools
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class GenerationBudget:
    max_requests: int
    max_reported_cost_usd: float
    requests: int = 0
    reported_cost_usd: float = 0

    def reserve(self):
        if self.requests >= self.max_requests or self.reported_cost_usd >= self.max_reported_cost_usd:
            raise ValueError('Generation budget exhausted. Review the partial result before retrying.')
        self.requests += 1


CURRENT_BUDGET: ContextVar[GenerationBudget | None] = ContextVar('generation_budget', default=None)


def generation_budget_spent() -> bool:
    """Whether the current run has no model request left to make."""
    budget = CURRENT_BUDGET.get()
    return budget is not None and (
        budget.requests >= budget.max_requests
        or budget.reported_cost_usd >= budget.max_reported_cost_usd
    )


def budgeted(function):
    @functools.wraps(function)
    async def wrapper(request, *args, **kwargs):
        if CURRENT_BUDGET.get() is not None:
            return await function(request, *args, **kwargs)
        options = request.options
        token = CURRENT_BUDGET.set(GenerationBudget(options.max_llm_requests, options.max_reported_cost_usd))
        try:
            async with asyncio.timeout(options.max_generation_seconds):
                return await function(request, *args, **kwargs)
        finally:
            CURRENT_BUDGET.reset(token)
    return wrapper
