"""The simulated broker's accounting against a hand-computed ledger.

Cash 1,000,000.00. Slippage 5 bps against the trader, commission 0.
  buy  100 NVDA, last 200.00 -> fill 200.10 -> cost 20,010.00 -> cash 979,990.00; pos 100 @ 200.10
  sell  40 NVDA, last 210.00 -> fill 209.895 -> proceeds 8,395.80; realised 40 x 9.795 = 391.80
                                 -> cash 988,385.80; pos 60 @ 200.10
  close    NVDA, last 190.00 -> fill 189.905 -> proceeds 11,394.30; realised 60 x -10.195 = -611.70
                                 -> cash 999,780.10; realised total -219.90; no position
Check: 979,990.00 + 8,395.80 + 11,394.30 = 999,780.10.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
import platform.models  # noqa: F401
import product.models  # noqa: F401
from platform.models import User
from product.exceptions import ConfirmationRequiredError, PaperOrderError
from product.flopx import domain
from product.flopx.paper import broker
from product.flopx.provenance import sample
from product.models import PaperOrder, PaperPosition


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
    user = User(name="Trader", email="trader@flopx.test", is_verified=True)
    db.add(user)
    db.commit()
    return broker.get_or_create_account(db, user.id)


class Tape:
    """A price lookup the test controls: ticker -> last close."""

    def __init__(self, **prices):
        self.prices = {k: Decimal(str(v)) for k, v in prices.items()}

    def __call__(self, ticker):
        if ticker not in self.prices:
            return None
        return domain.PriceObservation(
            series_id=f"equity:{ticker}:close", t=datetime(2026, 11, 6, 21, tzinfo=timezone.utc),
            value=float(self.prices[ticker]), unit="USD", provenance=sample(note="test tape"),
        )


def _cash(account) -> Decimal:
    return Decimal(account.cash_minor) / 100


def test_fresh_account_starts_with_one_million_and_no_margin(db, account):
    assert _cash(account) == Decimal("1000000.00")
    view = broker.summary(db, account, Tape())
    assert view.buying_power_usd == view.cash_usd == 1_000_000.0
    assert view.positions == [] and view.max_drawdown_pct == 0.0


def test_hand_computed_ledger_buy_partial_sell_close(db, account):
    tape = Tape(NVDA=200)
    buy = broker.place_order(db, account, ticker="NVDA", side="buy", quantity=100, price_lookup=tape)
    assert buy.status.value == "filled"
    assert Decimal(buy.fill_price) == Decimal("200.1000")
    assert _cash(account) == Decimal("979990.00")
    position = db.query(PaperPosition).filter_by(account_id=account.id, ticker="NVDA").one()
    assert (Decimal(position.quantity), Decimal(position.avg_cost)) == (Decimal("100"), Decimal("200.1000"))
    assert buy.fill_provenance["kind"] == "sample"   # a fill on a sample close says so

    tape.prices["NVDA"] = Decimal("210")
    sell = broker.place_order(db, account, ticker="NVDA", side="sell", quantity=40, price_lookup=tape)
    assert Decimal(sell.fill_price) == Decimal("209.8950")
    assert _cash(account) == Decimal("988385.80")
    assert Decimal(account.realised_pnl_minor) / 100 == Decimal("391.80")
    db.refresh(position)
    assert (Decimal(position.quantity), Decimal(position.avg_cost)) == (Decimal("60"), Decimal("200.1000"))

    tape.prices["NVDA"] = Decimal("190")
    close = broker.close_position(db, account, "NVDA", tape)
    assert Decimal(close.fill_price) == Decimal("189.9050")
    assert _cash(account) == Decimal("999780.10")
    assert Decimal(account.realised_pnl_minor) / 100 == Decimal("-219.90")
    assert db.query(PaperPosition).filter_by(account_id=account.id).count() == 0

    view = broker.summary(db, account, tape)
    assert view.realised_pnl_usd == -219.90 and view.unrealised_pnl_usd == 0.0
    # The equity peak was reached right after the partial sell, marked at 210:
    # 988,385.80 cash + 60 x 210 = 1,000,985.80. Drawdown from it to 999,780.10:
    # (1,000,985.80 - 999,780.10) / 1,000,985.80 = 0.120451%
    assert view.max_drawdown_pct == pytest.approx(0.120451, abs=1e-5)


def test_unrealised_pnl_and_drawdown_mark_to_the_last_price(db, account):
    tape = Tape(NVDA=200)
    broker.place_order(db, account, ticker="NVDA", side="buy", quantity=100, price_lookup=tape)
    tape.prices["NVDA"] = Decimal("190")
    view = broker.summary(db, account, tape)
    # 100 x (190 - 200.10) = -1,010.00; equity 979,990 + 19,000 = 998,990
    assert view.unrealised_pnl_usd == -1010.0
    assert view.equity_usd == 998_990.0
    assert view.max_drawdown_pct == pytest.approx(0.101, abs=1e-6)
    assert view.positions[0].weight == pytest.approx(19_000 / 998_990, rel=1e-6)


def test_insufficient_cash_is_a_persisted_rejection_with_reason(db, account):
    tape = Tape(NVDA=200)
    order = broker.place_order(db, account, ticker="NVDA", side="buy", quantity=10_000, price_lookup=tape)
    assert (order.status.value, order.reason) == ("rejected", "insufficient-cash")
    assert _cash(account) == Decimal("1000000.00")
    assert db.query(PaperOrder).filter_by(account_id=account.id, status="rejected").count() == 1


def test_selling_more_than_held_and_unknown_instruments_are_rejected(db, account):
    tape = Tape(NVDA=200)
    short = broker.place_order(db, account, ticker="NVDA", side="sell", quantity=1, price_lookup=tape)
    assert (short.status.value, short.reason) == ("rejected", "insufficient-position")
    unknown = broker.place_order(db, account, ticker="ZZZZ", side="buy", quantity=1, price_lookup=tape,
                                 instrument_exists=lambda t: t == "NVDA")
    assert (unknown.status.value, unknown.reason) == ("rejected", "unknown-instrument")
    no_data = broker.place_order(db, account, ticker="CRWV", side="buy", quantity=1, price_lookup=tape)
    assert (no_data.status.value, no_data.reason) == ("rejected", "market-data-unavailable")


def test_notional_orders_convert_at_the_fill_price(db, account):
    tape = Tape(NVDA=200)
    order = broker.place_order(db, account, ticker="NVDA", side="buy", notional_usd=10_000, price_lookup=tape)
    # 10,000 / 200.10 = 49.975012... floored to six decimals
    assert Decimal(order.quantity) == Decimal("49.975012")
    assert _cash(account) == Decimal("1000000.00") - (Decimal("49.975012") * Decimal("200.10")).quantize(Decimal("0.01"))


def test_limit_order_rests_until_marketable_and_can_be_cancelled(db, account):
    tape = Tape(NVDA=200)
    resting = broker.place_order(db, account, ticker="NVDA", side="buy", order_type="limit", quantity=10, limit_price=195, price_lookup=tape)
    assert resting.status.value == "accepted" and resting.fill_price is None
    assert _cash(account) == Decimal("1000000.00")
    # Still not marketable at 196 (fill would be 196.098 > 195):
    tape.prices["NVDA"] = Decimal("196")
    assert broker.settle_open_orders(db, account, tape) == []
    tape.prices["NVDA"] = Decimal("194")
    filled = broker.settle_open_orders(db, account, tape)
    assert [o.id for o in filled] == [resting.id] and Decimal(resting.fill_price) == Decimal("194.0970")

    second = broker.place_order(db, account, ticker="NVDA", side="buy", order_type="limit", quantity=10, limit_price=100, price_lookup=tape)
    cancelled = broker.cancel_order(db, account, second.id)
    assert cancelled.status.value == "cancelled"
    with pytest.raises(PaperOrderError):
        broker.cancel_order(db, account, second.id)


def test_close_all_needs_confirmation_then_flattens_everything(db, account):
    tape = Tape(NVDA=200, CRWV=100)
    broker.place_order(db, account, ticker="NVDA", side="buy", quantity=10, price_lookup=tape)
    broker.place_order(db, account, ticker="CRWV", side="buy", quantity=20, price_lookup=tape)

    with pytest.raises(ConfirmationRequiredError):
        broker.close_all(db, account, confirm=False, price_lookup=tape)
    assert db.query(PaperPosition).filter_by(account_id=account.id).count() == 2

    closed = broker.close_all(db, account, confirm=True, price_lookup=tape)
    assert sorted(o.ticker for o in closed) == ["CRWV", "NVDA"]
    assert all(o.status.value == "filled" for o in closed)
    assert db.query(PaperPosition).filter_by(account_id=account.id).count() == 0
    # Round trip at 5 bps each way: 10 x 200 x 0.001 + 20 x 100 x 0.001 = 4.00 lost to slippage
    assert _cash(account) == Decimal("999996.00")


def test_orders_carry_their_origin(db, account):
    tape = Tape(NVDA=200)
    manual = broker.place_order(db, account, ticker="NVDA", side="buy", quantity=1, price_lookup=tape)
    strategy = broker.place_order(db, account, ticker="NVDA", side="buy", quantity=1, origin="strategy:divergence", price_lookup=tape)
    assert (manual.origin, strategy.origin) == ("manual", "strategy:divergence")


def test_quantity_and_notional_are_exclusive(db, account):
    with pytest.raises(PaperOrderError):
        broker.place_order(db, account, ticker="NVDA", side="buy", quantity=1, notional_usd=100, price_lookup=Tape(NVDA=1))
    with pytest.raises(PaperOrderError):
        broker.place_order(db, account, ticker="NVDA", side="buy", price_lookup=Tape(NVDA=1))
