"""
Billing package — cost meter and budget enforcement (Phase 4, D7/D23).

Public surface: `price_usage` (pure math), `record_spend`/`window_spend`/
`usage_summary` (the ledger), and `check_budget`/`derive_run_caps` (pre-flight
policy). Money is integer micro-USD at rest, USD floats only at API
boundaries. No `pydantic_ai` imports (D3).
"""

from .budgets import BudgetDecision, RunCaps, check_budget, derive_run_caps
from .ledger import record_spend, usage_summary, window_spend, window_start
from .pricing import PessimisticMediaRates, PessimisticRates, PriceResult, price_media, price_usage


__all__ = [
    "BudgetDecision", "PessimisticMediaRates", "PessimisticRates", "PriceResult", "RunCaps",
    "check_budget", "derive_run_caps", "price_media", "price_usage",
    "record_spend", "usage_summary", "window_spend", "window_start",
]
