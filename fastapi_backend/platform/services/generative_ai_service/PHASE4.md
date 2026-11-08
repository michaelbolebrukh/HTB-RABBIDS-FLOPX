# Phase 4 — Cost Meter & Budgets: Full Development Spec

Status: spec for review (July 2026). Companion to `README.md` (D1–D31) and
`PHASE3.md` (registry — supplies the prices this phase multiplies). D7/D23 land
here: hard, in-process, transactional budget enforcement — blocks before spend,
never report-after.

## Goals / non-goals

**Goals**: price every run across all token dimensions; persist an append-only
spend ledger; enforce budgets per owner (and optionally per conversation/agent)
over time windows, checked BEFORE the model call; derive per-run token caps from
remaining budget; attribute summarizer (and later embedding) costs; surface cost
on `RunCompleted` and a minimal usage endpoint.

**Non-goals**: billing/invoicing UI, currency conversion (USD only), per-token
streaming cost ticker, provider spend reconciliation against invoices (future).

## Money representation (decision)

Costs are stored as **integer micro-USD** (`1_000_000 = $1.00`) in
`BigInteger` columns — exact, portable across PostgreSQL/SQLite (no
float/Numeric rounding drift), fast to SUM. `Decimal` at the API boundary,
integers at rest. Prices from the registry are Decimal-converted once in
`pricing.py`.

## New tables (package-owned, `ai_` prefix, portable types, one migration)

```python
class AISpendEvent(Base):
    """Append-only ledger: one row per priced unit of work."""
    __tablename__ = "ai_spend_events"
    id: Uuid pk
    owner_id: String, index                  # host-agnostic (D26)
    conversation_id: Uuid, nullable, index
    run_id: Uuid, nullable
    agent_name: String, nullable
    model_id: String                         # model that actually served (post-fallback)
    kind: String                             # "run" | "summarization" | "embedding"
    usage: JSONColumn                        # full Usage dict (all dimensions)
    cost_microusd: BigInteger, nullable      # None = unpriced (see policy)
    priced: Boolean, default True
    created_at: DateTime(tz), server_default now(), index
    # Composite index (owner_id, created_at) — the window-sum hot path.


class AIBudget(Base):
    """A spending limit for a scope over a rolling window."""
    __tablename__ = "ai_budgets"
    id: Uuid pk
    scope_type: String                       # "owner" | "conversation" | "agent" | "global"
    scope_id: String, index                  # owner_id / conversation uuid / agent name / "*"
    window: String                           # "daily" | "weekly" | "monthly" | "total"
    limit_microusd: BigInteger
    enabled: Boolean, default True
    meta: JSONColumn
    created_at / updated_at
    # Unique(scope_type, scope_id, window). Multiple windows may coexist
    # (e.g. $2/day AND $20/month — both enforced, strictest wins).
```

Platform defaults (env/settings): `AI_DEFAULT_OWNER_DAILY_BUDGET_USD`,
`AI_DEFAULT_OWNER_MONTHLY_BUDGET_USD`, `AI_BUDGET_ENFORCEMENT` (**off by
default — D32**: the service is informative out of the box; the consuming
product opts into blocking and defines its own limits/windows).
A missing `ai_budgets` row falls back to these defaults; an explicit row wins.

## Module layout (additions)

```
generative_ai_service/
  billing/
    __init__.py        # public: price_usage, record_spend, check_budget, window_spend
    pricing.py         # pure math: Usage x ModelInfo.cost -> PriceResult (no DB, no SDK)
    ledger.py          # session-first: record_spend, window_spend (SUM over ledger)
    budgets.py         # check_budget -> BudgetDecision; derive_run_caps
  models.py            # + the two tables above
  engine/engine.py     # pre-flight + post-run integration (small diff)
  engine/types.py      # + BudgetExceededError, RunCompleted.cost_usd
```

## Interfaces (normative)

```python
# pricing.py — pure, testable, zero I/O:
@dataclass
class PriceResult:
    cost_microusd: int | None      # None when unpriced
    priced: bool
    breakdown: dict[str, int]      # per-dimension micro-USD (input/output/cache_read/...)

def price_usage(usage: Usage, info: ModelInfo) -> PriceResult:
    """input/output/cache_read/cache_write/reasoning tokens x registry $/Mtok.
    Missing cost data -> unpriced result (policy applied by caller)."""


# ledger.py — session-first per repo rules:
def record_spend(db, *, owner_id, model_id, usage, price: PriceResult,
                 kind="run", conversation_id=None, run_id=None, agent_name=None) -> AISpendEvent
def window_spend(db, *, scope_type, scope_id, window) -> int   # micro-USD SUM


# budgets.py:
@dataclass
class BudgetDecision:
    allowed: bool
    remaining_microusd: int | None     # None = no budget configured (unlimited)
    binding_budget: str | None         # which scope/window blocked or binds
    run_caps: RunCaps                  # derived per-run ceilings (D23)

def check_budget(db, *, owner_id, conversation_id, agent_name) -> BudgetDecision:
    """Evaluating ALL applicable budgets (owner/conversation/agent/global x
    windows); strictest remaining wins. Zero/negative remaining -> not allowed."""

def derive_run_caps(remaining_microusd, info: ModelInfo, limits: AgentLimits) -> RunCaps:
    """Bounding worst-case run cost: converting remaining $ into a max-output-
    token cap at this model's output price (+ input headroom), then taking the
    min with the agent's own AgentLimits. Maps onto pydantic-ai UsageLimits."""
```

## Engine integration (the whole runtime diff)

In `stream_agent`, around the existing flow:

1. **Pre-flight** (before building/calling the model): `check_budget(...)`.
   - Not allowed → raise `BudgetExceededError` (typed; router maps to HTTP 429
     with remaining/window info). No provider call is ever made.
   - Allowed → merge `decision.run_caps` into the run's `UsageLimits`
     (min-combine with AgentLimits — D23 realized).
2. **Post-run** (after items persist): `price_usage(outcome.usage,
   info_for(served_model))` → `record_spend(...)` → `RunCompleted.cost_usd`
   populated. Usage comes from the authoritative RunUsage, which already
   includes retry requests (D7's retry-token requirement is satisfied by
   construction).
3. **Summarizer attribution** (O6.8): `make_summarizer` extended to return
   usage alongside text; the engine records a `kind="summarization"` spend
   event against the conversation owner. Same later for embeddings (P5).

## Semantics decided here (call out for review)

- **Unpriced model policy** — a model the registry has no cost for runs under a
  **pessimistic default rate** (configurable, e.g. $15/$75 per Mtok in/out):
  the spend event is recorded `priced=False` with the pessimistic cost counted
  against budgets, and a Sentry **warning** fires once per model per process.
  Rationale: unknown models must not be free riders that drain real money
  invisibly; blocking would break day-one model use (P3's conservative-default
  promise). Configurable to `block` for stricter deployments.
- **Concurrency honesty** — two concurrent runs can both pass pre-flight and
  jointly overshoot a nearly-exhausted budget. Bounded overshoot = (concurrent
  runs) x (per-run cap), because run_caps bound each run's worst case. We accept
  this documented bound instead of cross-run locking; the ledger is the truth
  and the next pre-flight blocks.
- **Cancellation undercount** — a run cancelled mid-stream may lose the
  provider-side usage of the in-flight request (provider still bills it).
  Known, documented undercount; mitigation (post-hoc reconciliation) deferred.
- **Budget exceeded is NOT a Sentry error** — it is expected, user-facing
  behavior (429 path), per the repo observability rule. Sentry fires for:
  unpriced-model runs (warning), spend-write failure after a successful run
  (error — money data lost), and pre-flight infrastructure failure (error,
  fail-CLOSED when enforcement is on: if we cannot verify budget, we do not
  spend).
- **Fail-closed choice** — with `AI_BUDGET_ENFORCEMENT=on`, a broken budget
  check blocks the run (403/503-style) rather than allowing unmetered spend.
  This is the opposite of the cache's fail-open rule, deliberately: cache
  failure costs latency; budget failure costs money.

## Surfacing

- `RunCompleted.cost_usd: float | None` (and `priced: bool`) — additive,
  non-breaking event change; UIs can show per-turn cost immediately.
- `GET /ai/usage` — owner-scoped summary: spend today/this month, per-model
  breakdown (simple GROUP BY over the ledger). Minimal now; dashboards later.

## Tests (offline, deterministic)

- Pricing golden tests per dimension (incl. cache read/write and reasoning;
  micro-USD exactness; unpriced → None).
- Ledger: record + window sums with injected timestamps (no wall-clock flake).
- Budgets: default-from-settings vs explicit row; multiple windows (strictest
  wins); scope precedence; zero-remaining blocks; run-caps derivation math and
  min-combination with AgentLimits.
- Engine: pre-flight block raises before any adapter call (asserted via a
  sentinel model); post-run spend recorded with correct attribution; summarizer
  spend event; unpriced-model pessimistic path; fail-closed on budget-check
  exception; RunCompleted carries cost.
- Router: 429 mapping with remaining-budget payload.

## Migration

One migration: `ai_spend_events` + `ai_budgets`, idempotent guards (the
create_all-then-migrate rule), upgrade + downgrade, portable ops.

## Done criteria

A run against a priced model debits the ledger with an exact micro-USD cost and
returns it on `RunCompleted`; an owner over budget gets a 429 without any
provider call; per-run caps shrink as budget depletes; summarization costs are
attributed; unpriced models cost pessimistically and warn; all tests green; no
`pydantic_ai` import outside the two sanctioned files.
