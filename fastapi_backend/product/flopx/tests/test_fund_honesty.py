"""The fund's figures say what they are built from (falsifiers V1 and V3, 2026-11-07).

- one drawdown computation for the fund and the desk, counting the first fill's slippage;
- the account value carries the provenance of its marks (SAMPLE when a mark is sample);
- the equity curve's observation time is its newest input's, never the request time;
- each proposed position carries provenance and a stated risk.

Expected numbers are worked by hand in the comments, not recomputed with the code under test.
"""

import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401
import product.models  # noqa: F401
from platform.models import User
from product.enums import DecisionAction, StrategyMode
from product.flopx import domain, fund, strategies
from product.flopx.paper import broker
from product.flopx.provenance import Provenance, sample
from product.models import StrategyDecision

TAPE_AT = datetime(2026, 11, 6, 21, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def account(db):
    user = User(name="Fund", email="fund-honesty@flopx.test", is_verified=True)
    db.add(user)
    db.commit()
    return broker.get_or_create_account(db, user.id)


class Tape:
    def __init__(self, **prices):
        self.prices = {k: Decimal(str(v)) for k, v in prices.items()}

    def __call__(self, ticker):
        if ticker not in self.prices:
            return None
        return domain.PriceObservation(series_id=f"equity:{ticker}:close", t=TAPE_AT, value=float(self.prices[ticker]), unit="USD",
                                       provenance=sample(observed_at=TAPE_AT, note="test tape"))


def _series(ticker: str, values: list[float], end: datetime) -> domain.Series:
    start = end - timedelta(days=len(values) - 1)
    prov = sample(observed_at=end, note="test closes")
    return domain.Series(id=f"equity:{ticker}:close", unit="USD",
                         points=[domain.PriceObservation(f"equity:{ticker}:close", start + timedelta(days=i), v, "USD", prov) for i, v in enumerate(values)])


def _summary(db, account, series, decisions=(), as_of=TAPE_AT + timedelta(hours=3), tape=None):
    clusters = [fund.Cluster(id="gpu-cloud", name="GPU cloud", tickers=["NVDA"])]
    return fund.summary(db, account, series=series, price_lookup=tape or Tape(NVDA=200), clusters=clusters,
                        clusters_provenance=sample(), decisions=list(decisions), tickers=["NVDA"], as_of=as_of)


# -- drawdown: one computation --------------------------------------------------------------------------

def test_the_running_peak_starts_at_starting_cash_so_first_fill_slippage_is_a_drawdown():
    # Start 1,000,000; the only curve point is 999,950 (5 bps paid on a 100,000 buy): 50 / 1,000,000 = 0.005%.
    t = datetime(2026, 11, 6, tzinfo=timezone.utc)
    rows, worst = fund.drawdowns([{"t": t, "value_usd": 999_950.0}], 1_000_000.0)
    assert worst == pytest.approx(0.005)
    assert rows == [{"t": t, "drawdown_pct": 0.005}]


def test_the_current_equity_is_the_last_point_of_the_drawdown_path():
    # Peak 1,010,000 on the curve, current equity 999,900: (1,010,000 - 999,900) / 1,010,000 = 1.0%.
    t = datetime(2026, 11, 6, tzinfo=timezone.utc)
    _, worst = fund.drawdowns([{"t": t, "value_usd": 1_010_000.0}], 1_000_000.0, current_equity_usd=999_900.0)
    assert worst == pytest.approx(1.0)


def test_the_desk_and_the_fund_report_the_same_max_drawdown(db, account):
    tape = Tape(NVDA=200)
    broker.place_order(db, account, ticker="NVDA", side="buy", quantity=100, price_lookup=tape)   # fill 200.10, cash 979,990
    series = {"equity:NVDA:close": _series("NVDA", [200.0] * 5, TAPE_AT)}
    summary = _summary(db, account, series, tape=tape)
    # Value after the fill: 979,990 + 100 x 200 = 999,990; drawdown 10 / 1,000,000 = 0.001%.
    assert summary["max_drawdown_pct"] == pytest.approx(0.001)
    assert summary["account"]["max_drawdown_pct"] == summary["max_drawdown_pct"]
    acct = broker.summary(db, account, tape)
    desk = fund.account_max_drawdown_pct(db, account, series, TAPE_AT + timedelta(hours=3), acct.equity_usd)
    assert desk == summary["max_drawdown_pct"]


# -- provenance of the account value and of the curve ---------------------------------------------------

def test_an_account_marked_to_a_sample_close_carries_sample_provenance(db, account):
    tape = Tape(NVDA=200)
    broker.place_order(db, account, ticker="NVDA", side="buy", quantity=10, price_lookup=tape)
    summary = _summary(db, account, {"equity:NVDA:close": _series("NVDA", [200.0] * 5, TAPE_AT)}, tape=tape)
    prov = summary["account_provenance"]
    assert isinstance(prov, Provenance) and prov.kind == "derived" and prov.input_kinds == ("sample",)
    assert prov.observed_at == TAPE_AT


def test_an_account_of_cash_alone_has_no_mark_provenance(db, account):
    summary = _summary(db, account, {})
    assert summary["account_provenance"] is None


def test_the_equity_curve_is_observed_at_its_newest_input_not_at_the_request(db, account):
    tape = Tape(NVDA=200)
    broker.place_order(db, account, ticker="NVDA", side="buy", quantity=10, price_lookup=tape)
    as_of = TAPE_AT + timedelta(days=2)
    summary = _summary(db, account, {"equity:NVDA:close": _series("NVDA", [200.0] * 5, TAPE_AT)}, as_of=as_of, tape=tape)
    prov = summary["equity_curve_provenance"]
    assert prov.kind == "derived" and "sample" in prov.input_kinds
    # The newest input is the last close (and the fill priced on it), both observed at TAPE_AT.
    assert prov.observed_at == TAPE_AT and prov.observed_at < as_of


# -- proposed positions: provenance and stated risk ------------------------------------------------------

def test_target_risk_is_the_one_sigma_move_over_the_horizon():
    # 61 closes whose log returns alternate +0.01 and -0.01: 60 returns, mean 0, sample stdev
    # sqrt(60 x 0.0001 / 59) = 0.01 x sqrt(60/59). A 14-day horizon is round(14 x 5/7) = 10 trading days.
    values = [100.0 * math.exp(0.01 if i % 2 else 0.0) for i in range(61)]
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    series = {"equity:NVDA:close": _series("NVDA", values, end)}
    risk, provs = fund.target_risk(series, "NVDA", end, 14, 50_000.0)
    daily = 0.01 * math.sqrt(60 / 59)
    assert risk["horizon_trading_days"] == 10 and risk["closes"] == 61
    assert risk["daily_volatility_pct"] == pytest.approx(daily * 100, abs=1e-4)
    assert risk["one_sigma_pct"] == pytest.approx(daily * math.sqrt(10) * 100, abs=1e-4)
    assert risk["one_sigma_usd"] == pytest.approx(50_000.0 * daily * math.sqrt(10), abs=0.01)
    assert provs and all(p.kind == "sample" for p in provs)


def test_target_risk_uses_only_closes_known_at_the_decision():
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    series = {"equity:NVDA:close": _series("NVDA", [100.0] * 40 + [100.0, 200.0] * 15, end)}
    at = end - timedelta(days=30)                                    # before the volatile tail
    risk, _ = fund.target_risk(series, "NVDA", at, 20, 50_000.0)
    assert risk["daily_volatility_pct"] == 0.0


def test_target_risk_is_absent_without_enough_closes():
    end = datetime(2026, 10, 1, tzinfo=timezone.utc)
    risk, _ = fund.target_risk({"equity:NVDA:close": _series("NVDA", [100.0] * 5, end)}, "NVDA", end, 20, 50_000.0)
    assert risk is None


def test_each_proposed_position_carries_sample_provenance_and_its_risk(db, account):
    t = datetime(2026, 10, 1, tzinfo=timezone.utc)
    row = StrategyDecision(strategy_id="divergence", account_id=account.id, t=t, action=DecisionAction.BUY, ticker="NVDA", rationale={"z": 2.0},
                           known_data_snapshot={}, mode=StrategyMode.FORWARD, acted=False, status="proposed", signal_id="signal:divergence:NVDA:2026-10-01",
                           signal_kind="price-change", horizon_days=20, proposed_weight=0.05)
    db.add(row)
    db.commit()
    values = [100.0 * math.exp(0.01 if i % 2 else 0.0) for i in range(61)]
    summary = _summary(db, account, {"equity:NVDA:close": _series("NVDA", values, t)}, decisions=[row])
    [target] = summary["target_positions"]
    assert target["provenance"].kind == "derived" and target["provenance"].input_kinds == ("sample",)
    assert target["risk"]["notional_usd"] == 50_000.0 and target["risk"]["horizon_trading_days"] == 14   # round(20 x 5/7)
    assert target["expires_at"] == t + timedelta(days=20)


# -- attribution: FIFO lots by the origin that opened them (falsifier V4 F9) -----------------------------

def test_a_manual_round_trip_has_no_open_value_and_its_true_realised_pnl(db, account):
    # buy 100 NVDA at 200 -> fill 200.10, cost 20,010.00; close at 200 -> fill 199.90, proceeds 19,990.00.
    # Realised 100 x (199.90 - 200.10) = -20.00. Nothing open.
    broker.place_order(db, account, ticker="NVDA", side="buy", quantity=100, price_lookup=Tape(NVDA=200))
    broker.close_position(db, account, "NVDA", Tape(NVDA=200))
    summary = _summary(db, account, {"equity:NVDA:close": _series("NVDA", [200.0] * 5, TAPE_AT)}, tape=Tape(NVDA=200))
    assert summary["attribution_by_origin"] == {"manual": {"fills": 2, "bought_usd": 20_010.0, "sold_usd": 19_990.0, "open_value_usd": 0.0,
                                                           "realised_pnl_usd": -20.0, "unrealised_pnl_usd": 0.0, "pnl_usd": -20.0, "unpriced": []}}
    assert summary["fills"] == 2 and summary["account"]["positions"] == []


def test_a_strategy_lot_closed_by_hand_counts_to_the_strategy(db, account):
    # Strategy buy 100 at 200 -> fill 200.10 (cost 20,010.00); manual close at 190 -> fill 189.905
    # (proceeds 18,990.50). Realised 100 x (189.905 - 200.10) = -1,019.50, all the strategy's.
    broker.place_order(db, account, ticker="NVDA", side="buy", quantity=100, origin="strategy:divergence", price_lookup=Tape(NVDA=200))
    broker.close_position(db, account, "NVDA", Tape(NVDA=190))
    summary = _summary(db, account, {"equity:NVDA:close": _series("NVDA", [190.0] * 5, TAPE_AT)}, tape=Tape(NVDA=190))
    by_origin = summary["attribution_by_origin"]
    assert set(by_origin) == {"strategy:divergence"}
    strat = by_origin["strategy:divergence"]
    assert strat["open_value_usd"] == 0.0 and strat["realised_pnl_usd"] == -1019.5 and strat["pnl_usd"] == -1019.5
    assert strat["bought_usd"] == 20_010.0 and strat["sold_usd"] == 18_990.5 and strat["fills"] == 2


def test_partial_sells_consume_the_oldest_lot_first(db, account):
    # Manual lot 10 @ 100.05 (tape 100), then strategy lot 10 @ 120.06 (tape 120); sell 15 at tape 110 -> 109.945.
    # FIFO: manual 10 x (109.945 - 100.05) = 98.95 realised; strategy 5 x (109.945 - 120.06) = -50.575 -> -50.58.
    # Strategy keeps 5 open at mark 110: value 550.00, unrealised 5 x (110 - 120.06) = -50.30.
    broker.place_order(db, account, ticker="NVDA", side="buy", quantity=10, price_lookup=Tape(NVDA=100))
    broker.place_order(db, account, ticker="NVDA", side="buy", quantity=10, origin="strategy:supply-regime", price_lookup=Tape(NVDA=120))
    broker.place_order(db, account, ticker="NVDA", side="sell", quantity=15, price_lookup=Tape(NVDA=110))
    summary = _summary(db, account, {"equity:NVDA:close": _series("NVDA", [110.0] * 5, TAPE_AT)}, tape=Tape(NVDA=110))
    manual, strat = summary["attribution_by_origin"]["manual"], summary["attribution_by_origin"]["strategy:supply-regime"]
    assert manual["realised_pnl_usd"] == 98.95 and manual["open_value_usd"] == 0.0 and manual["pnl_usd"] == 98.95
    assert strat["realised_pnl_usd"] == pytest.approx(-50.58, abs=0.01) and strat["open_value_usd"] == 550.0
    assert strat["unrealised_pnl_usd"] == pytest.approx(-50.30, abs=0.01)
