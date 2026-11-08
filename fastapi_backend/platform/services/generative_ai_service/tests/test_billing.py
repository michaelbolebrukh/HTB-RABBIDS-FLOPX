"""
Billing tests (Phase 4): pricing golden values, ledger window sums with
injected timestamps, budget evaluation/derivation, and the engine's
pre-flight + spend-recording integration. All offline and deterministic.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from platform.services.generative_ai_service.billing import (
    PessimisticRates,
    check_budget,
    derive_run_caps,
    price_usage,
    record_spend,
    usage_summary,
    window_spend,
    window_start,
)
from platform.services.generative_ai_service.models import AIBudget, AISpendEvent
from platform.services.generative_ai_service.registry import Cost, ModelInfo
from platform.services.generative_ai_service.transcript.types import Usage


# A fixed "now" so window math never flakes (mid-month, mid-week, mid-day UTC):
NOW = datetime(2026, 7, 15, 12, 0, 0, tzinfo=timezone.utc)

# A model priced like claude-sonnet: $3/Mtok in, $15/Mtok out, $0.30 cache read:
PRICED = ModelInfo(id="t:m", provider="t",
                   cost=Cost(input=3.0, output=15.0, cache_read=0.3, cache_write=3.75))
UNPRICED = ModelInfo(id="t:unknown", provider="t")


# ============================================================================
# PRICING (golden values — micro-USD exactness)
# ============================================================================

def test_price_usage_golden_all_dimensions():
    usage = Usage(input_tokens=1_000_000, output_tokens=100_000,
                  cache_read_tokens=500_000, cache_write_tokens=10_000)
    result = price_usage(usage, PRICED)
    # 1M in x $3 = $3.00; 100k out x $15 = $1.50; 500k cache-read x $0.30 = $0.15;
    # 10k cache-write x $3.75 = $0.0375 -> total $4.6875 = 4_687_500 micro-USD:
    assert result.priced is True
    assert result.breakdown == {"input": 3_000_000, "output": 1_500_000,
                                "cache_read": 150_000, "cache_write": 37_500}
    assert result.cost_microusd == 4_687_500


def test_reasoning_only_priced_with_dedicated_rate():
    usage = Usage(input_tokens=1000, output_tokens=1000, reasoning_tokens=5000)
    # No reasoning rate -> reasoning already inside output; must not double-count:
    assert "reasoning" not in price_usage(usage, PRICED).breakdown
    # With a dedicated rate it is priced separately:
    with_rate = ModelInfo(id="t:r", provider="t", cost=Cost(input=3.0, output=15.0, reasoning=10.0))
    assert price_usage(usage, with_rate).breakdown["reasoning"] == 50_000


def test_unpriced_model_policies():
    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    # No pessimistic rates -> unpriced result:
    assert price_usage(usage, UNPRICED).cost_microusd is None
    # With pessimistic rates -> costed but flagged unpriced ($15/Mtok default):
    result = price_usage(usage, UNPRICED, pessimistic=PessimisticRates())
    assert result.cost_microusd == 15_000_000 and result.priced is False


# ============================================================================
# LEDGER (window sums with injected timestamps)
# ============================================================================

def _spend(db, owner, micro, when, **kw):
    """Recording one deterministic ledger row for tests:"""
    from platform.services.generative_ai_service.billing.pricing import PriceResult
    return record_spend(
        db, owner_id=owner, model_id=kw.pop("model_id", "t:m"), usage=Usage(),
        price=PriceResult(cost_microusd=micro, priced=True), created_at=when, **kw,
    )


def test_window_start_boundaries():
    assert window_start("daily", NOW) == datetime(2026, 7, 15, tzinfo=timezone.utc)
    assert window_start("weekly", NOW) == datetime(2026, 7, 13, tzinfo=timezone.utc)   # Monday
    assert window_start("monthly", NOW) == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert window_start("total", NOW) is None


def test_window_spend_scopes_and_windows(db):
    conv = uuid4()
    _spend(db, "u1", 1_000_000, NOW)                                       # today
    _spend(db, "u1", 2_000_000, datetime(2026, 7, 2, tzinfo=timezone.utc))  # this month, not today
    _spend(db, "u1", 4_000_000, datetime(2026, 6, 2, tzinfo=timezone.utc))  # last month
    _spend(db, "u2", 8_000_000, NOW, conversation_id=conv)                 # another owner

    assert window_spend(db, scope_type="owner", scope_id="u1", window="daily", now=NOW) == 1_000_000
    assert window_spend(db, scope_type="owner", scope_id="u1", window="monthly", now=NOW) == 3_000_000
    assert window_spend(db, scope_type="owner", scope_id="u1", window="total", now=NOW) == 7_000_000
    assert window_spend(db, scope_type="conversation", scope_id=str(conv), window="daily", now=NOW) == 8_000_000
    assert window_spend(db, scope_type="global", scope_id="*", window="daily", now=NOW) == 9_000_000


def test_usage_summary_shape(db):
    _spend(db, "u1", 1_500_000, NOW, model_id="openai:gpt-5.2")
    report = usage_summary(db, "u1", now=NOW)
    assert report["today_usd"] == 1.5 and report["month_by_model"] == {"openai:gpt-5.2": 1.5}


# ============================================================================
# BUDGETS (evaluation, strictest-wins, caps derivation)
# ============================================================================

def test_no_budgets_means_unlimited(db):
    decision = check_budget(db, owner_id="u1", now=NOW)
    assert decision.allowed and decision.remaining_microusd is None
    assert derive_run_caps(None, PRICED).max_output_tokens is None


def test_strictest_budget_wins_and_blocks(db):
    # Two windows on one owner: $10 monthly, $1 daily (strictest):
    db.add(AIBudget(scope_type="owner", scope_id="u1", window="monthly", limit_microusd=10_000_000))
    db.add(AIBudget(scope_type="owner", scope_id="u1", window="daily", limit_microusd=1_000_000))
    db.commit()

    _spend(db, "u1", 400_000, NOW)   # $0.40 spent today
    decision = check_budget(db, owner_id="u1", now=NOW)
    assert decision.allowed and decision.remaining_microusd == 600_000
    assert decision.binding_budget == "owner/daily"

    _spend(db, "u1", 600_000, NOW)   # Daily now exhausted
    decision = check_budget(db, owner_id="u1", now=NOW)
    assert not decision.allowed and decision.binding_budget == "owner/daily"


def test_derive_run_caps_math():
    # $1.00 remaining, output at $15/Mtok -> half the money buys ~33_333 tokens:
    caps = derive_run_caps(1_000_000, PRICED)
    assert caps.max_output_tokens == 33_333
    # Near-empty budgets still allow a small useful response (the floor):
    assert derive_run_caps(100, PRICED).max_output_tokens == 128
    # Unpriced models cap at pessimistic output rates ($75/Mtok):
    assert derive_run_caps(1_500_000, UNPRICED).max_output_tokens == 10_000


def test_disabled_budget_rows_are_ignored(db):
    db.add(AIBudget(scope_type="owner", scope_id="u1", window="daily",
                    limit_microusd=1, enabled=False))
    db.commit()
    assert check_budget(db, owner_id="u1", now=NOW).allowed


# ============================================================================
# ENGINE INTEGRATION (pre-flight blocks; spend recorded after a run)
# ============================================================================

@pytest.mark.asyncio
async def test_engine_preflight_blocks_before_any_side_effect(db, monkeypatch):
    from infrastructure.settings import settings

    from platform.services.generative_ai_service.engine import engine as engine_mod
    from platform.services.generative_ai_service.engine import AgentSpec, RunDeps, UserInput
    from platform.services.generative_ai_service.transcript import create_conversation, full_history

    # Enforcement is opt-in (D32: informative by default) — opting in like a product would:
    monkeypatch.setattr(settings, "AI_BUDGET_ENFORCEMENT", True)

    conversation = create_conversation(db, owner_id="u1")
    # An exhausted budget (limit 1 micro-USD, already spent more):
    db.add(AIBudget(scope_type="owner", scope_id="u1", window="total", limit_microusd=1))
    db.commit()
    _spend(db, "u1", 5, NOW)

    spec = AgentSpec(name="a", model="test:test")
    deps = RunDeps(db=db, owner_id="u1", conversation_id=conversation.id)

    events = []
    async for event in engine_mod.stream_agent(spec, deps, UserInput(text="hi")):
        events.append(event)

    # The run failed at pre-flight AND persisted nothing (no user item):
    assert events[-1].type == "run_failed" and "budget" in events[-1].error.lower()
    assert full_history(db, conversation.id, owner_id="u1") == []


@pytest.mark.asyncio
async def test_engine_records_spend_after_test_model_run(db, monkeypatch):
    from pydantic_ai.models.test import TestModel

    from platform.services.generative_ai_service.engine import engine as engine_mod
    from platform.services.generative_ai_service.engine import AgentSpec, RunDeps, UserInput
    from platform.services.generative_ai_service.engine.providers import BuiltModel
    from platform.services.generative_ai_service.transcript import create_conversation

    # Replacing build_model with a TestModel factory (the suite's offline pattern):
    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: BuiltModel(model=TestModel(), requested_id=model_id, served_by="test"),
    )

    conversation = create_conversation(db, owner_id="u9")
    spec = AgentSpec(name="a", model="test:test")   # Resolved to TestModel above
    deps = RunDeps(db=db, owner_id="u9", conversation_id=conversation.id)

    completed = None
    async for event in engine_mod.stream_agent(spec, deps, UserInput(text="hello")):
        if event.type == "run_completed":
            completed = event

    # The run completed and a ledger row exists for the owner (TestModel is
    # unknown to the registry -> pessimistic pricing, priced=False):
    assert completed is not None
    rows = db.query(AISpendEvent).filter_by(owner_id="u9").all()
    assert len(rows) == 1 and rows[0].kind == "run"
    assert rows[0].priced is False and rows[0].cost_microusd > 0
    assert completed.cost_usd is not None and completed.priced is False


@pytest.mark.asyncio
async def test_default_posture_is_informative_not_restrictive(db, monkeypatch):
    """D32: with default settings, exhausted budgets NEVER block — but cost is
    still tracked (ledger row + RunCompleted.cost_usd). Enforcement is opt-in."""
    from pydantic_ai.models.test import TestModel

    from platform.services.generative_ai_service.engine import engine as engine_mod
    from platform.services.generative_ai_service.engine import AgentSpec, RunDeps, UserInput
    from platform.services.generative_ai_service.engine.providers import BuiltModel
    from platform.services.generative_ai_service.transcript import create_conversation

    monkeypatch.setattr(
        engine_mod, "build_model",
        lambda model_id, fallbacks=None: BuiltModel(model=TestModel(), requested_id=model_id, served_by="test"),
    )

    conversation = create_conversation(db, owner_id="u7")
    # An utterly exhausted budget exists — but enforcement was never opted into:
    db.add(AIBudget(scope_type="owner", scope_id="u7", window="total", limit_microusd=1))
    db.commit()
    _spend(db, "u7", 999_999, NOW)

    spec = AgentSpec(name="a", model="test:test")
    deps = RunDeps(db=db, owner_id="u7", conversation_id=conversation.id)

    completed = None
    async for event in engine_mod.stream_agent(spec, deps, UserInput(text="hi")):
        assert event.type != "run_failed", f"informative mode must not block: {event}"
        if event.type == "run_completed":
            completed = event

    # The run proceeded AND its cost was still tracked (informative posture) —
    # the engine's row carries a run_id, distinguishing it from the seeded row:
    assert completed is not None and completed.cost_usd is not None
    engine_rows = [r for r in db.query(AISpendEvent).filter_by(owner_id="u7").all() if r.run_id]
    assert len(engine_rows) == 1
