"""A replayed decision is hindsight and is never presented as forward evidence (falsifier R2, 2026-11-07).

- R2-06: a filled replay counted under the strategy's forward paper results, and each replay of
  one date added another decision to one signal;
- R2-39: a forward step silently superseded an open replayed proposal;
- R2-33: a replay of a lapsed date was recorded as `proposed`, and the strategy view kept a
  lapsed proposal at `proposed` until a fund read ran expiry;
- R2-21 / R2-10: event-reaction said "no-qualifying-event" when the event existed and its ticker
  was held, and a held ticker's forward hold was skipped as "no actionable signal".

Real sqlite and the real broker with a test tape, as the decision-ledger tests.
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
from product.flopx import domain, fund, strategies, terminal
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
    user = User(name="Replay", email="replay@flopx.test", is_verified=True)
    db.add(user)
    db.commit()
    return broker.get_or_create_account(db, user.id)


class Tape:
    def __init__(self, **prices):
        self.prices = {k: Decimal(str(v)) for k, v in prices.items()}

    def __call__(self, ticker):
        if ticker not in self.prices:
            return None
        return domain.PriceObservation(series_id=f"equity:{ticker}:close", t=datetime(2026, 11, 6, 21, tzinfo=timezone.utc), value=float(self.prices[ticker]),
                                       unit="USD", provenance=sample(note="test tape"))


def _first_buy_date(spec, data):
    for point in data.series[strategies.INDEX_SERIES].points[61:]:
        if strategies.decide(spec, data, point.t).action == DecisionAction.BUY.value:
            return point.t
    raise AssertionError(f"{spec.id} never buys over the sample window")


def _replay(db, spec, data, account, at):
    """A replay taken one day after the replayed date: inside every strategy's horizon."""
    return strategies.step_forward(db, spec, data, account, at=at, replay=True, now=at + timedelta(days=1))[0]


# -- R2-06: one replay per date, marked, and outside the forward results ------------------------------------

def test_replaying_a_date_with_an_open_replay_returns_that_decision(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    first = _replay(db, spec, data, account, at)
    again = _replay(db, spec, data, account, at + timedelta(hours=2))          # same calendar date
    assert first.status == "proposed"
    assert again.id == first.id
    assert db.query(StrategyDecision).count() == 1


def test_replaying_a_date_with_a_filled_replay_returns_the_filled_decision(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row = _replay(db, spec, data, account, at)
    accepted, order = strategies.accept_decision(db, spec, account, row, Tape(**{row.ticker: 100.0}), now=at + timedelta(days=1))
    assert accepted.status == "filled"
    again = _replay(db, spec, data, account, at)
    assert again.id == row.id and again.status == "filled"
    assert db.query(StrategyDecision).count() == 1 and db.query(PaperOrder).count() == 1


def test_a_replay_fill_carries_a_replay_origin_and_a_forward_fill_a_strategy_origin(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row = _replay(db, spec, data, account, at)
    _, order = strategies.accept_decision(db, spec, account, row, Tape(**{row.ticker: 100.0}), now=at + timedelta(days=1))
    assert order.origin == "replay:event-reaction"
    assert strategies.replay_of(row) == at.date().isoformat()


def test_the_forward_summary_lists_a_replay_but_does_not_count_it(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row = _replay(db, spec, data, account, at)
    strategies.accept_decision(db, spec, account, row, Tape(**{row.ticker: 100.0}), now=at + timedelta(days=1))
    payload = terminal._strategy_payload(db, data, spec, account, with_backtest=False)["forward"]
    assert len(payload["decisions"]) == 1
    assert payload["decisions"][0]["replay_of"] == at.date().isoformat()
    assert payload["orders"] == 0 and payload["proposed"] == 0 and payload["replays"] == 1


def test_a_replay_fill_is_attributed_to_the_replay_group_not_the_strategy(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row = _replay(db, spec, data, account, at)
    _, order = strategies.accept_decision(db, spec, account, row, Tape(**{row.ticker: 100.0}), now=at + timedelta(days=1))
    # A fill placed before replay origins existed: the decision, not the order's origin, says it was a replay.
    order.origin = "strategy:event-reaction"
    db.commit()
    db.refresh(row)
    out = fund.summary(db, account, series=data.series, price_lookup=Tape(**{row.ticker: 100.0}), clusters=[], clusters_provenance=sample(),
                       decisions=[row], tickers=[row.ticker], as_of=datetime(2026, 11, 7, tzinfo=timezone.utc))
    assert set(out["attribution_by_origin"]) == {"replay:event-reaction"}
    assert set(out["attribution_by_signal_kind"]) == {"replay"}


# -- R2-39: forward steps and replays are separate ledgers ---------------------------------------------------

def test_a_forward_step_never_supersedes_an_open_replay(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    replayed = _replay(db, spec, data, account, at)
    strategies.step_forward(db, spec, data, account, at=at + timedelta(days=1))        # a forward step (hold or proposal)
    db.refresh(replayed)
    assert replayed.status == "proposed"


def test_a_replay_never_supersedes_an_open_forward_proposal(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    forward, _ = strategies.step_forward(db, spec, data, account, at=at)
    assert forward.status == "proposed"
    _replay(db, spec, data, account, at - timedelta(days=30))
    db.refresh(forward)
    assert forward.status == "proposed"


def test_a_replay_of_another_date_supersedes_the_earlier_open_replay(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    first = _replay(db, spec, data, account, at)
    strategies.step_forward(db, spec, data, account, at=at + timedelta(days=1), replay=True, now=at + timedelta(days=2))
    db.refresh(first)
    assert first.status == "skipped" and first.skipped_reason == "superseded"


# -- R2-33: a lapsed proposal never reads proposed -----------------------------------------------------------

def test_a_replay_of_a_date_whose_horizon_has_passed_is_recorded_expired(db, data, account):
    spec = strategies.STRATEGIES["event-reaction"]                                       # 10-day horizon
    at = _first_buy_date(spec, data)
    row, _ = strategies.step_forward(db, spec, data, account, at=at, replay=True, now=at + timedelta(days=11))
    assert row.action == DecisionAction.BUY
    assert row.status == "skipped" and row.skipped_reason == "expired"
    again, _ = strategies.step_forward(db, spec, data, account, at=at, replay=True, now=at + timedelta(days=12))
    assert again.id == row.id and db.query(StrategyDecision).count() == 1


def test_the_strategy_view_runs_expiry(db, data, account, monkeypatch):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    row = _replay(db, spec, data, account, at)                                          # proposed, and the sample date is months ago
    monkeypatch.setattr(terminal, "_data", lambda: data)
    user = db.query(User).filter(User.id == account.user_id).one()
    view = terminal.strategy_view(db, spec.id, user)
    statuses = {d["id"]: d["status"] for d in view["forward"]["decisions"]}
    assert statuses[row.id] == "skipped"
    db.refresh(row)
    assert row.skipped_reason == "expired"


# -- R2-21 / R2-10: holds say why they held ------------------------------------------------------------------

def test_event_reaction_says_already_held_when_the_qualifying_event_names_a_held_ticker(data):
    spec = strategies.STRATEGIES["event-reaction"]
    at = _first_buy_date(spec, data)
    bought = strategies.decide(spec, data, at)
    named = {t for item in data.feed if item.id == bought.rationale["itemId"] for t in item.entities.tickers if t in spec.universe}
    held = strategies.decide(spec, data, at, holding={t: at for t in named})            # entered now: no hold-period sell
    assert held.action == DecisionAction.HOLD.value
    assert held.rationale["signal"] == "already-held" and held.rationale["itemId"] == bought.rationale["itemId"]
    assert held.ticker in named


def test_event_reaction_with_no_event_still_says_no_qualifying_event(data):
    spec = strategies.STRATEGIES["event-reaction"]
    first = data.series[strategies.INDEX_SERIES].points[0].t - timedelta(days=400)        # long before any feed item
    assert strategies.decide(spec, data, first).rationale["signal"] == "no-qualifying-event"


def test_a_forward_hold_on_a_held_ticker_is_skipped_as_already_held(db, data, account):
    spec = strategies.STRATEGIES["divergence"]
    at = _first_buy_date(spec, data)
    broker.place_order(db, account, ticker="CRWV", side="buy", notional_usd=1_000.0, price_lookup=Tape(CRWV=100.0))
    row, _ = strategies.step_forward(db, spec, data, account, at=at)
    assert row.action == DecisionAction.HOLD and row.status == "skipped" and row.skipped_reason == "already-held"
