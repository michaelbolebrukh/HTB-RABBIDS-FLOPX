"""The decision ledger (architecture §10.7): a step proposes, only an accept trades.

Real sqlite, the real broker with a test tape. The strategy is stepped at the
first sample date on which its rule says BUY, so the row is `proposed`; the
orders table must stay empty until `accept_decision` runs, and the row's status
must follow what the broker did.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401
import product.models  # noqa: F401
from platform.models import User
from product.enums import DecisionAction
from product.exceptions import DecisionConflictError, PaperOrderError
from product.flopx import domain, signals, strategies
from product.flopx.data.loader import load_sample_uncached
from product.flopx.paper import broker
from product.flopx.provenance import sample
from product.models import PaperOrder, StrategyDecision


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="module")
def data():
    return load_sample_uncached()


@pytest.fixture
def account(db):
    user = User(name="Fund", email="fund@flopx.test", is_verified=True)
    db.add(user)
    db.commit()
    return broker.get_or_create_account(db, user.id)


class Tape:
    def __init__(self, **prices):
        self.prices = {k: Decimal(str(v)) for k, v in prices.items()}

    def __call__(self, ticker):
        if ticker not in self.prices:
            return None
        return domain.PriceObservation(series_id=f"equity:{ticker}:close", t=datetime(2026, 11, 6, 21, tzinfo=timezone.utc), value=float(self.prices[ticker]), unit="USD",
                                       provenance=sample(note="test tape"))


def _first_buy_date(spec, data):
    for point in data.series[strategies.INDEX_SERIES].points[61:]:
        if strategies.decide(spec, data, point.t).action == DecisionAction.BUY.value:
            return point.t
    raise AssertionError(f"{spec.id} never buys over the sample window")


def _soon(at):
    """A moment inside the proposal's horizon: these tests accept a proposal while it is current.
    The sample's buy dates lie months before today, so the real clock would expire them."""
    return at + timedelta(days=1)


def test_a_step_proposes_and_creates_no_order_until_accept(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row, order = strategies.step_forward(db, spec, data, account, at=at)
    assert order is None
    assert row.status == "proposed" and row.action == DecisionAction.BUY and row.ticker and row.order_ids is None and row.acted is False
    assert row.signal_id == strategies.signal_id_for(spec.id, row.ticker, at) and row.signal_kind == "event" and row.horizon_days == 10 and row.proposed_weight == 0.05
    assert db.query(PaperOrder).count() == 0
    assert account.cash_minor == broker.INITIAL_CASH_MINOR

    tape = Tape(**{row.ticker: 100.0})
    accepted, placed = strategies.accept_decision(db, spec, account, row, tape, now=_soon(at))
    assert placed is not None and placed.status.value == "filled" and placed.origin == "strategy:event-reaction"
    assert accepted.status == "filled" and accepted.acted is True and accepted.order_ids == [str(placed.id)]
    assert db.query(PaperOrder).count() == 1 and account.cash_minor < broker.INITIAL_CASH_MINOR

    with pytest.raises(PaperOrderError) as refused:
        strategies.accept_decision(db, spec, account, accepted, tape, now=_soon(at))
    assert refused.value.details["reason"] == "decision-not-proposed"
    assert db.query(PaperOrder).count() == 1


def test_a_rejected_broker_answer_marks_the_decision_rejected(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row, _ = strategies.step_forward(db, spec, data, account, at=at)
    accepted, placed = strategies.accept_decision(db, spec, account, row, Tape(), now=_soon(at))          # no price for the ticker
    assert placed.status.value == "rejected" and placed.reason == "market-data-unavailable"
    assert accepted.status == "rejected" and accepted.skipped_reason == "market-data-unavailable" and accepted.acted is False


def test_a_hold_is_recorded_as_skipped_with_its_reason(db, data, account):
    spec = strategies.STRATEGIES["supply-regime"]
    early = data.series[strategies.INDEX_SERIES].points[10].t                                # fewer than 60 index points known: insufficient history
    row, _ = strategies.step_forward(db, spec, data, account, at=early)
    assert row.status == "skipped" and row.skipped_reason == "insufficient-sample" and row.signal_id is None and row.action == DecisionAction.HOLD
    later = data.series[strategies.INDEX_SERIES].points[-1].t
    row2, _ = strategies.step_forward(db, spec, data, account, at=later)
    assert row2.status in ("skipped", "proposed")
    if row2.status == "skipped":
        assert row2.skipped_reason == "no-actionable-signal"


def test_accept_refuses_another_accounts_decision_and_applies_the_risk_limit(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row, _ = strategies.step_forward(db, spec, data, account, at=at)
    other_user = User(name="Other", email="other@flopx.test", is_verified=True)
    db.add(other_user)
    db.commit()
    other = broker.get_or_create_account(db, other_user.id)
    with pytest.raises(PaperOrderError) as refused:
        strategies.accept_decision(db, spec, other, row, Tape(**{row.ticker: 100.0}), now=_soon(at))
    assert refused.value.details["reason"] == "decision-not-owned"
    broker.place_order(db, account, ticker=row.ticker, side="buy", quantity=1, price_lookup=Tape(**{row.ticker: 100.0}))
    accepted, placed = strategies.accept_decision(db, spec, account, row, Tape(**{row.ticker: 100.0}), now=_soon(at))
    assert placed is None and accepted.status == "skipped" and accepted.skipped_reason == "risk-limit"


def test_the_signal_chain_reads_the_ledger(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row, _ = strategies.step_forward(db, spec, data, account, at=at)
    strategies.accept_decision(db, spec, account, row, Tape(**{row.ticker: 100.0}), now=_soon(at))
    rows = strategies.forward_decisions(db, None, account, limit=50)
    orders = {str(o.id): o for o in broker.list_orders(db, account)}
    chain = signals.from_decisions(rows, data.series, orders, strategies=strategies.STRATEGIES)
    assert len(chain) == 1
    signal = chain[0]
    assert signal.id == row.signal_id and signal.kind == "event" and signal.skipped is None
    assert [d.status for d in signal.decision_refs] == ["filled"] and [o.status for o in signal.orders] == ["filled"]
    assert signal.forecast_ref is not None and signal.forecast_ref["horizon_days"] == 10
    assert signal.outcome is not None and signal.outcome["horizon_days"] == 10        # the sample closes run past at + 10 days
    assert signal.connected[0]["instrument"] == row.ticker


def test_price_only_variants_and_costs_are_distinct_and_labelled(data):
    spec = strategies.STRATEGIES["divergence"]
    compute = strategies.backtest(spec, data, cost_bps=5.0)
    price_only = strategies.backtest(spec, data, variant="price-only", cost_bps=5.0)
    free = strategies.backtest(spec, data)
    assert compute.variant == "compute" and price_only.variant == "price-only" and compute.cost_bps == 5.0
    assert compute.costs_usd > 0 and free.costs_usd == 0 and compute.total_return_pct < free.total_return_pct
    assert price_only.decisions[-1].known_data_snapshot["driver"] == "equity:CRWV:close" and compute.decisions[-1].known_data_snapshot["driver"] == strategies.INDEX_SERIES
    assert "sample" in compute.provenance.input_kinds and compute.statement_class == "decision"
    with pytest.raises(ValueError):
        strategies.decide(spec, data, data.series[strategies.INDEX_SERIES].points[-1].t, variant="nonsense")


# -- falsifier V3 (2026-11-07): stale, superseded, mismatched and racing accepts ----------------------

def test_a_proposal_past_its_horizon_expires_and_cannot_be_accepted(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]                                         # 10-day horizon
    at = _first_buy_date(spec, data)
    row, _ = strategies.step_forward(db, spec, data, account, at=at)
    with pytest.raises(PaperOrderError) as refused:
        strategies.accept_decision(db, spec, account, row, Tape(**{row.ticker: 100.0}), now=at + timedelta(days=11))
    assert refused.value.details["reason"] == "decision-expired"
    db.refresh(row)
    assert row.status == "skipped" and row.skipped_reason == "expired" and row.order_ids is None
    assert db.query(PaperOrder).count() == 0


def test_expiry_leaves_a_proposal_inside_its_horizon_alone(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row, _ = strategies.step_forward(db, spec, data, account, at=at)
    assert strategies.expire_proposals(db, account, now=at + timedelta(days=9)) == 0
    db.refresh(row)
    assert row.status == "proposed"
    assert strategies.expire_proposals(db, account, now=at + timedelta(days=10, seconds=1)) == 1


def test_the_next_step_supersedes_the_earlier_proposal(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    first, _ = strategies.step_forward(db, spec, data, account, at=at)
    second, _ = strategies.step_forward(db, spec, data, account, at=at + timedelta(hours=1))
    db.refresh(first)
    assert first.status == "skipped" and first.skipped_reason == "superseded"
    assert second.status == "proposed"
    with pytest.raises(PaperOrderError) as refused:
        strategies.accept_decision(db, spec, account, first, Tape(**{first.ticker: 100.0}), now=_soon(at))
    assert refused.value.details["reason"] == "decision-superseded"
    assert db.query(PaperOrder).count() == 0


def test_a_step_supersedes_only_its_own_strategy(db, data, account):
    event = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(event, data)
    row, _ = strategies.step_forward(db, event, data, account, at=at)
    strategies.step_forward(db, strategies.STRATEGIES["supply-regime"], data, account, at=at)
    db.refresh(row)
    assert row.status == "proposed"


def test_accepting_under_the_wrong_strategy_names_the_mismatch(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row, _ = strategies.step_forward(db, spec, data, account, at=at)
    with pytest.raises(PaperOrderError) as refused:
        strategies.accept_decision(db, strategies.STRATEGIES["divergence"], account, row, Tape(**{row.ticker: 100.0}), now=_soon(at))
    assert refused.value.details["reason"] == "decision-strategy-mismatch"
    db.refresh(row)
    assert row.status == "proposed"


def test_an_accept_racing_a_claimed_decision_is_a_conflict_and_places_nothing(db, data, account):
    # The state a concurrent winner leaves between its claim and its fill: status `submitted`.
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row, _ = strategies.step_forward(db, spec, data, account, at=at)
    db.query(StrategyDecision).filter(StrategyDecision.id == row.id).update({StrategyDecision.status: "submitted"}, synchronize_session=False)
    db.commit()
    with pytest.raises(DecisionConflictError) as refused:
        strategies.accept_decision(db, spec, account, row, Tape(**{row.ticker: 100.0}), now=_soon(at))
    assert refused.value.details["reason"] == "decision-not-proposed"
    assert db.query(PaperOrder).count() == 0


def test_a_broker_failure_after_the_claim_hands_the_proposal_back(db, data, account, monkeypatch):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row, _ = strategies.step_forward(db, spec, data, account, at=at)

    def boom(*args, **kwargs):
        raise RuntimeError("broker write failed")

    monkeypatch.setattr(strategies.broker, "place_order", boom)
    with pytest.raises(RuntimeError):
        strategies.accept_decision(db, spec, account, row, Tape(**{row.ticker: 100.0}), now=_soon(at))
    db.refresh(row)
    assert row.status == "proposed" and row.order_ids is None


def test_a_replayed_step_says_which_date_it_replays(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row, _ = strategies.step_forward(db, spec, data, account, at=at, replay=True)
    assert row.rationale["replayOf"] == at.date().isoformat()
    plain, _ = strategies.step_forward(db, spec, data, account, at=at)
    assert "replayOf" not in plain.rationale


def test_a_hold_rationale_says_why_it_held():
    # Above the buy threshold but already held is not "within the band" (falsifier V3 F14).
    assert strategies._hold_reason(1.875, 1.5, -1.5, held=True) == "already-held"
    assert strategies._hold_reason(-2.0, 1.5, -1.5, held=False) == "nothing-held"
    assert strategies._hold_reason(0.3, 1.5, -1.5, held=True) == "within-band"
    assert strategies._hold_reason(0.3, 1.5, -1.5, held=False) == "within-band"
    assert strategies._hold_reason(1.0, 1.05, 0.95, held=False, inside="no-regime-shift") == "no-regime-shift"


def test_divergence_reports_already_held_when_the_signal_fires_on_a_held_ticker(data):
    spec = strategies.STRATEGIES["divergence"]
    at = _first_buy_date(spec, data)
    held = strategies.decide(spec, data, at, holding={"CRWV": at})
    assert held.action == DecisionAction.HOLD.value and held.rationale["signal"] == "already-held"
    assert held.known_data_snapshot["z"] > spec.parameters["enter_z"]
