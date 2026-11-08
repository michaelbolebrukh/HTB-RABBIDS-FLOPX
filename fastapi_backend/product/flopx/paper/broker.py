"""Simulated broker: accounts, orders, fills, positions, P&L, drawdown.

Rules (architecture §6), all of them in this one module:
- one account per user, cash 1,000,000 USD, buying power = cash, no margin,
  no shorting;
- market and limit orders, buy/sell, quantity OR notional, day time-in-force;
- a market order fills immediately at the last observed close for the ticker
  plus 5 bps slippage against the trader (buy pays more, sell receives less),
  commission 0; a limit order fills immediately when marketable, otherwise
  rests as `accepted` and is re-evaluated on the next read;
- rejections are persisted with a reason token: `insufficient-cash`,
  `insufficient-position`, `unknown-instrument`, `market-data-unavailable`;
- every order carries `origin`: "manual" or "strategy:<id>"; every fill carries
  the provenance of the price it used, so a fill on a sample close says so.

Money is Decimal: cash in integer cents, prices to four decimals, quantities to
six. `price_lookup(ticker)` is injected (the router wires Yahoo-with-sample-
fallback; tests wire a dict), which is what keeps this module free of network.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, Decimal
from typing import Callable, Iterable, Optional

from sqlalchemy.orm import Session

from product.enums import OrderSide, OrderStatus, OrderType
from product.exceptions import ConfirmationRequiredError, PaperOrderError
from product.flopx import domain
from product.flopx.provenance import Provenance
from product.models import PaperAccount, PaperOrder, PaperPosition

INITIAL_CASH_MINOR = 100_000_000           # 1,000,000.00 USD
SLIPPAGE_BPS = 5
COMMISSION_MINOR = 0

CENT = Decimal("0.01")
PRICE_Q = Decimal("0.0001")
QTY_Q = Decimal("0.000001")

PriceLookup = Callable[[str], Optional[domain.PriceObservation]]
InstrumentCheck = Callable[[str], bool]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _minor(amount_usd: Decimal) -> int:
    return int((amount_usd * 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


def usd(minor: int) -> float:
    return float(Decimal(minor) / 100)


# -- accounts -------------------------------------------------------------------

def get_or_create_account(db: Session, user_id: int) -> PaperAccount:
    account = db.query(PaperAccount).filter(PaperAccount.user_id == user_id).first()
    if account is not None:
        return account
    account = PaperAccount(user_id=user_id, cash_minor=INITIAL_CASH_MINOR, realised_pnl_minor=0,
                           peak_equity_minor=INITIAL_CASH_MINOR)
    db.add(account)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(account)
    return account


def positions(db: Session, account: PaperAccount) -> list[PaperPosition]:
    return db.query(PaperPosition).filter(PaperPosition.account_id == account.id).order_by(PaperPosition.ticker).all()


def list_orders(db: Session, account: PaperAccount, limit: int = 200) -> list[PaperOrder]:
    return (
        db.query(PaperOrder)
        .filter(PaperOrder.account_id == account.id)
        .order_by(PaperOrder.created_at.desc(), PaperOrder.id)
        .limit(limit)
        .all()
    )


def open_orders(db: Session, account: PaperAccount) -> list[PaperOrder]:
    return (
        db.query(PaperOrder)
        .filter(PaperOrder.account_id == account.id, PaperOrder.status == OrderStatus.ACCEPTED)
        .order_by(PaperOrder.created_at)
        .all()
    )


# -- fills ------------------------------------------------------------------------

def fill_price_for(last: Decimal, side: OrderSide) -> Decimal:
    slip = last * Decimal(SLIPPAGE_BPS) / Decimal(10_000)
    price = last + slip if side == OrderSide.BUY else last - slip
    return price.quantize(PRICE_Q, rounding=ROUND_HALF_EVEN)


def _position(db: Session, account: PaperAccount, ticker: str) -> PaperPosition | None:
    return db.query(PaperPosition).filter(PaperPosition.account_id == account.id, PaperPosition.ticker == ticker).first()


def _apply_fill(db: Session, account: PaperAccount, order: PaperOrder, price: Decimal, provenance: Provenance, at: datetime) -> None:
    """Moves cash and the position for a fill; the caller commits."""
    quantity = _dec(order.quantity)
    gross = (quantity * price).quantize(CENT, rounding=ROUND_HALF_EVEN)
    position = _position(db, account, order.ticker)

    if order.side == OrderSide.BUY:
        account.cash_minor -= _minor(gross) + COMMISSION_MINOR
        if position is None:
            db.add(PaperPosition(account_id=account.id, ticker=order.ticker, quantity=quantity, avg_cost=price))
        else:
            old_qty, old_avg = _dec(position.quantity), _dec(position.avg_cost)
            new_qty = old_qty + quantity
            position.avg_cost = ((old_qty * old_avg + quantity * price) / new_qty).quantize(PRICE_Q, rounding=ROUND_HALF_EVEN)
            position.quantity = new_qty
    else:
        account.cash_minor += _minor(gross) - COMMISSION_MINOR
        realised = (quantity * (price - _dec(position.avg_cost))).quantize(CENT, rounding=ROUND_HALF_EVEN)
        account.realised_pnl_minor += _minor(realised)
        remaining = _dec(position.quantity) - quantity
        if remaining <= 0:
            db.delete(position)
        else:
            position.quantity = remaining

    order.status = OrderStatus.FILLED
    order.fill_price = price
    order.fill_provenance = provenance.to_dict()
    order.filled_at = at


def _reject(db: Session, order: PaperOrder, reason: str) -> PaperOrder:
    order.status = OrderStatus.REJECTED
    order.reason = reason
    db.add(order)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(order)
    return order


def place_order(
    db: Session,
    account: PaperAccount,
    *,
    ticker: str,
    side: str,
    order_type: str = "market",
    quantity: float | Decimal | None = None,
    notional_usd: float | Decimal | None = None,
    limit_price: float | Decimal | None = None,
    origin: str = "manual",
    price_lookup: PriceLookup,
    instrument_exists: InstrumentCheck = lambda ticker: True,
) -> PaperOrder:
    """Validates, then fills or rests or rejects. Never raises for a business refusal:
    the refusal is the persisted rejected order (so the desk can show it)."""
    side_e, type_e = OrderSide(side), OrderType(order_type)
    if (quantity is None) == (notional_usd is None):
        raise PaperOrderError(reason="quantity-xor-notional")
    if type_e == OrderType.LIMIT and limit_price is None:
        raise PaperOrderError(reason="limit-price-required")
    ticker = ticker.upper()
    now = _now()

    order = PaperOrder(
        id=uuid.uuid4(), account_id=account.id, ticker=ticker, side=side_e, order_type=type_e,
        quantity=_dec(quantity).quantize(QTY_Q, rounding=ROUND_DOWN) if quantity is not None else Decimal(0),
        notional_minor=_minor(_dec(notional_usd)) if notional_usd is not None else None,
        limit_price=_dec(limit_price).quantize(PRICE_Q) if limit_price is not None else None,
        status=OrderStatus.PENDING, origin=origin, created_at=now,
    )

    if not instrument_exists(ticker):
        return _reject(db, order, "unknown-instrument")
    last = price_lookup(ticker)
    if last is None:
        return _reject(db, order, "market-data-unavailable")

    fill = fill_price_for(_dec(last.value), side_e)
    if notional_usd is not None:
        order.quantity = (_dec(notional_usd) / fill).quantize(QTY_Q, rounding=ROUND_DOWN)
        if order.quantity <= 0:
            return _reject(db, order, "notional-too-small")

    if side_e == OrderSide.SELL:
        held = _position(db, account, ticker)
        if held is None or _dec(held.quantity) < _dec(order.quantity):
            return _reject(db, order, "insufficient-position")

    marketable = (
        type_e == OrderType.MARKET
        or (side_e == OrderSide.BUY and fill <= _dec(order.limit_price))
        or (side_e == OrderSide.SELL and fill >= _dec(order.limit_price))
    )
    if side_e == OrderSide.BUY and marketable:
        cost = _minor((_dec(order.quantity) * fill).quantize(CENT, rounding=ROUND_HALF_EVEN)) + COMMISSION_MINOR
        if cost > account.cash_minor:
            return _reject(db, order, "insufficient-cash")

    order.status = OrderStatus.ACCEPTED
    db.add(order)
    if marketable:
        _apply_fill(db, account, order, fill, last.provenance, now)
        # The session is autoflush=False: flush so the peak is computed over the
        # positions as they now are (a just-deleted position must not count):
        db.flush()
        _touch_peak(db, account, price_lookup)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(order)
    return order


def settle_open_orders(db: Session, account: PaperAccount, price_lookup: PriceLookup) -> list[PaperOrder]:
    """Re-evaluates resting limit orders against current prices; returns the ones that filled."""
    filled = []
    for order in open_orders(db, account):
        last = price_lookup(order.ticker)
        if last is None:
            continue
        fill = fill_price_for(_dec(last.value), order.side)
        if order.side == OrderSide.BUY and fill <= _dec(order.limit_price):
            cost = _minor((_dec(order.quantity) * fill).quantize(CENT))
            if cost > account.cash_minor:
                order.status, order.reason = OrderStatus.REJECTED, "insufficient-cash"
                continue
            _apply_fill(db, account, order, fill, last.provenance, _now())
            filled.append(order)
        elif order.side == OrderSide.SELL and fill >= _dec(order.limit_price):
            held = _position(db, account, order.ticker)
            if held is None or _dec(held.quantity) < _dec(order.quantity):
                order.status, order.reason = OrderStatus.REJECTED, "insufficient-position"
                continue
            _apply_fill(db, account, order, fill, last.provenance, _now())
            filled.append(order)
    db.flush()
    if filled:
        _touch_peak(db, account, price_lookup)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    return filled


def cancel_order(db: Session, account: PaperAccount, order_id: uuid.UUID) -> PaperOrder:
    order = db.query(PaperOrder).filter(PaperOrder.id == order_id, PaperOrder.account_id == account.id).first()
    if order is None:
        raise PaperOrderError(reason="unknown-order", order_id=str(order_id))
    if order.status not in (OrderStatus.PENDING, OrderStatus.ACCEPTED):
        raise PaperOrderError(reason="not-cancellable", status=order.status.value)
    order.status = OrderStatus.CANCELLED
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(order)
    return order


def close_position(db: Session, account: PaperAccount, ticker: str, price_lookup: PriceLookup, *, origin: str = "manual") -> PaperOrder:
    held = _position(db, account, ticker.upper())
    if held is None:
        raise PaperOrderError(reason="no-position", ticker=ticker.upper())
    return place_order(db, account, ticker=ticker, side="sell", order_type="market",
                       quantity=_dec(held.quantity), origin=origin, price_lookup=price_lookup)


def close_all(db: Session, account: PaperAccount, *, confirm: bool, price_lookup: PriceLookup) -> list[PaperOrder]:
    """Closes every position. Refuses without `confirm=True`; nothing is touched then."""
    if not confirm:
        raise ConfirmationRequiredError(action="close-all")
    orders = []
    for held in positions(db, account):
        orders.append(close_position(db, account, held.ticker, price_lookup))
    return orders


# -- valuation -------------------------------------------------------------------

@dataclass(frozen=True)
class PositionView:
    ticker: str
    quantity: float
    avg_cost: float
    last_price: float | None
    market_value_usd: float | None
    unrealised_pnl_usd: float | None
    weight: float | None
    price_provenance: Provenance | None


@dataclass(frozen=True)
class AccountSummary:
    cash_usd: float
    equity_usd: float
    buying_power_usd: float
    realised_pnl_usd: float
    unrealised_pnl_usd: float
    max_drawdown_pct: float
    positions: list[PositionView]
    exposure_by_theme: dict[str, float] = field(default_factory=dict)
    open_orders: int = 0
    provenances: list[Provenance] = field(default_factory=list)


def _equity_minor(db: Session, account: PaperAccount, price_lookup: PriceLookup) -> int:
    total = Decimal(account.cash_minor)
    for held in positions(db, account):
        last = price_lookup(held.ticker)
        price = _dec(last.value) if last is not None else _dec(held.avg_cost)
        total += (_dec(held.quantity) * price * 100).quantize(Decimal("1"), rounding=ROUND_HALF_EVEN)
    return int(total)


def _touch_peak(db: Session, account: PaperAccount, price_lookup: PriceLookup) -> int:
    equity = _equity_minor(db, account, price_lookup)
    if equity > account.peak_equity_minor:
        account.peak_equity_minor = equity
    return equity


def summary(
    db: Session,
    account: PaperAccount,
    price_lookup: PriceLookup,
    *,
    theme_lookup: Callable[[str], Iterable[str]] | None = None,
) -> AccountSummary:
    settle_open_orders(db, account, price_lookup)
    views: list[PositionView] = []
    provs: list[Provenance] = []
    unrealised = Decimal(0)
    for held in positions(db, account):
        last = price_lookup(held.ticker)
        qty, avg = _dec(held.quantity), _dec(held.avg_cost)
        if last is not None:
            price = _dec(last.value)
            value = (qty * price).quantize(CENT)
            pnl = (qty * (price - avg)).quantize(CENT)
            unrealised += pnl
            provs.append(last.provenance)
            views.append(PositionView(held.ticker, float(qty), float(avg), float(price), float(value), float(pnl), None, last.provenance))
        else:
            views.append(PositionView(held.ticker, float(qty), float(avg), None, None, None, None, None))

    equity_minor = _touch_peak(db, account, price_lookup)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    equity = Decimal(equity_minor) / 100
    weighted = []
    themes: dict[str, Decimal] = {}
    for view in views:
        weight = (Decimal(str(view.market_value_usd)) / equity) if (view.market_value_usd is not None and equity) else None
        weighted.append(PositionView(**{**view.__dict__, "weight": float(weight) if weight is not None else None}))
        if weight is not None and theme_lookup is not None:
            for theme in theme_lookup(view.ticker):
                themes[theme] = themes.get(theme, Decimal(0)) + weight
    peak = Decimal(account.peak_equity_minor) / 100
    drawdown = float((peak - equity) / peak * 100) if peak > 0 and equity < peak else 0.0
    return AccountSummary(
        cash_usd=usd(account.cash_minor),
        equity_usd=float(equity),
        buying_power_usd=usd(account.cash_minor),
        realised_pnl_usd=usd(account.realised_pnl_minor),
        unrealised_pnl_usd=float(unrealised),
        max_drawdown_pct=round(drawdown, 6),
        positions=weighted,
        exposure_by_theme={k: round(float(v), 4) for k, v in sorted(themes.items())},
        open_orders=len(open_orders(db, account)),
        provenances=provs,
    )


def order_view(order: PaperOrder) -> dict:
    """The wire shape of an order (plain data, no ORM instance leaves the service)."""
    return {
        "id": str(order.id),
        "ticker": order.ticker,
        "side": order.side.value if hasattr(order.side, "value") else order.side,
        "order_type": order.order_type.value if hasattr(order.order_type, "value") else order.order_type,
        "quantity": float(order.quantity or 0),
        "notional_usd": usd(order.notional_minor) if order.notional_minor is not None else None,
        "limit_price": float(order.limit_price) if order.limit_price is not None else None,
        "status": order.status.value if hasattr(order.status, "value") else order.status,
        "origin": order.origin,
        "reason": order.reason,
        "fill_price": float(order.fill_price) if order.fill_price is not None else None,
        "fill_provenance": order.fill_provenance,
        "filled_at": order.filled_at,
        "created_at": order.created_at,
    }
