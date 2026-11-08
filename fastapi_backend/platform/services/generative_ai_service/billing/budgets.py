"""
Budgets — pre-flight enforcement policy (Phase 4, D7/D23).

`check_budget` evaluates EVERY applicable budget (explicit `ai_budgets` rows
for owner/conversation/agent/global scopes, plus settings-level owner defaults
where no explicit row exists) and returns the strictest remaining allowance.
`derive_run_caps` then converts remaining dollars into per-run token ceilings
so a single run can never blow through what's left (D23).

Concurrency honesty (documented in PHASE4): two concurrent runs can both pass
pre-flight; the overshoot is bounded by (concurrent runs x per-run cap). The
ledger is the truth and the next pre-flight blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from infrastructure.settings import settings

from ..models import AIBudget
from ..registry import ModelInfo
from .ledger import window_spend
from .pricing import PessimisticRates


# The fraction of remaining budget one run may consume as OUTPUT tokens — the
# rest is headroom for input/cache dimensions the cap can't directly bound:
_OUTPUT_BUDGET_FRACTION = 0.5

# A tiny floor so a nearly-exhausted (but positive) budget still allows a
# useful final response instead of a zero-token cap:
_MIN_OUTPUT_CAP_TOKENS = 128


@dataclass
class RunCaps:
    """Per-run ceilings derived from remaining budget (None = no budget bound)."""
    max_output_tokens: int | None = None


@dataclass
class BudgetDecision:
    """The pre-flight verdict: may this run proceed, and under what caps."""
    allowed: bool
    remaining_microusd: int | None       # None = no budget configured (unlimited)
    binding_budget: str | None           # "owner/daily" etc. — which limit binds/blocks
    run_caps: RunCaps


def _applicable_budgets(db: Session, owner_id: str, conversation_id: UUID | None, agent_name: str | None):
    """
    Collecting (scope_type, scope_id, window, limit) for every budget that
    applies: explicit enabled rows for each scope, plus settings defaults for
    the owner scope where no explicit row covers that window:
    """
    scopes: list[tuple[str, str]] = [("owner", owner_id), ("global", "*")]
    if conversation_id is not None:
        scopes.append(("conversation", str(conversation_id)))
    if agent_name:
        scopes.append(("agent", agent_name))

    budgets: list[tuple[str, str, str, int]] = []
    covered_owner_windows: set[str] = set()

    for scope_type, scope_id in scopes:
        rows = db.execute(
            select(AIBudget).where(
                AIBudget.scope_type == scope_type,
                AIBudget.scope_id == scope_id,
                AIBudget.enabled.is_(True),
            )
        ).scalars().all()
        for row in rows:
            budgets.append((scope_type, scope_id, row.window, int(row.limit_microusd)))
            if scope_type == "owner":
                covered_owner_windows.add(row.window)

    # Settings-level owner defaults fill windows no explicit row covers:
    defaults = (
        ("daily", getattr(settings, "AI_DEFAULT_OWNER_DAILY_BUDGET_USD", None)),
        ("monthly", getattr(settings, "AI_DEFAULT_OWNER_MONTHLY_BUDGET_USD", None)),
    )
    for window, usd in defaults:
        if usd is not None and window not in covered_owner_windows:
            budgets.append(("owner", owner_id, window, int(float(usd) * 1_000_000)))

    return budgets


def check_budget(
    db: Session,
    *,
    owner_id: str,
    conversation_id: UUID | None = None,
    agent_name: str | None = None,
    now: datetime | None = None,
) -> BudgetDecision:
    """
    Evaluating all applicable budgets; the strictest remaining wins. No
    budgets at all -> unlimited (allowed, remaining None). Zero or negative
    remaining on ANY budget -> blocked, naming the binding budget:
    """
    budgets = _applicable_budgets(db, owner_id, conversation_id, agent_name)
    if not budgets:
        return BudgetDecision(allowed=True, remaining_microusd=None, binding_budget=None, run_caps=RunCaps())

    strictest_remaining: int | None = None
    binding: str | None = None
    for scope_type, scope_id, window, limit in budgets:
        spent = window_spend(db, scope_type=scope_type, scope_id=scope_id, window=window, now=now)
        remaining = limit - spent
        if strictest_remaining is None or remaining < strictest_remaining:
            strictest_remaining = remaining
            binding = f"{scope_type}/{window}"

    if strictest_remaining is not None and strictest_remaining <= 0:
        return BudgetDecision(allowed=False, remaining_microusd=0, binding_budget=binding, run_caps=RunCaps())

    return BudgetDecision(
        allowed=True, remaining_microusd=strictest_remaining, binding_budget=binding, run_caps=RunCaps(),
    )


def derive_run_caps(remaining_microusd: int | None, info: ModelInfo) -> RunCaps:
    """
    Converting remaining budget into a per-run output-token ceiling (D23):
    half the remaining money buys output tokens at this model's output rate
    (pessimistic rate for unpriced models), floored so a near-empty budget
    still permits a small useful response. None remaining = no bound:
    """
    if remaining_microusd is None:
        return RunCaps()

    # USD-per-Mtok == micro-USD per token; unpriced models cap at pessimistic rates:
    output_rate = info.cost.output if info.cost.output else PessimisticRates().output
    affordable = int((remaining_microusd * _OUTPUT_BUDGET_FRACTION) / max(output_rate, 0.000001))
    return RunCaps(max_output_tokens=max(affordable, _MIN_OUTPUT_CAP_TOKENS))
