"""Three transparent rule strategies with point-in-time backtests and a decision ledger.

Each strategy is a rule over data the model already has; nothing is fitted.
`decide()` is given only what was knowable at the decision time (series cut at
t, feed items observed by t), and every decision records the exact values it
used (`known_data_snapshot`) so a reader can recompute it, plus the SIGNAL it
acted on (architecture §10.7): a dated, sourced change with a kind and a
magnitude, so the chain signal -> forecast -> decision -> orders -> outcome can
be walked. Backtests run over the SAMPLE history and are labelled `backtest`
with derived-from-sample provenance; they can charge costs per fill and can run
a `price-only` variant of each rule (the compute feature replaced by the asset's
own momentum) so the compute information's contribution is visible.

Forward steps PROPOSE: `step_forward()` records a `StrategyDecision` with
status `proposed` and places nothing. `accept_decision()` is the one path that
submits a paper order (origin `strategy:<id>`), and the row only reads
`submitted`, then `filled` or `rejected`, from what the broker actually did.
Nothing is ever shown as executed before a fill; a decision the rule did not
act on is `skipped` with its reason. No realised profit is claimed beyond the
simulated ledger.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from product.enums import DecisionAction, StrategyMode
from product.exceptions import DecisionConflictError, PaperOrderError
from product.flopx import domain
from product.flopx.data.loader import SampleData
from product.flopx.paper import broker
from product.flopx.provenance import Provenance, derived, effective_kind, utcnow
from product.flopx.series import point_in_time
from product.models import PaperAccount, PaperOrder, StrategyDecision

INDEX_SERIES = "gpu:h100-sxm-80gb:on-demand:global"   # the FCPI H100 history is this series rescaled
ORDER_NOTIONAL_USD = 50_000.0
ACCOUNT_REFERENCE_USD = 1_000_000.0
PROPOSED_WEIGHT = ORDER_NOTIONAL_USD / ACCOUNT_REFERENCE_USD
DECISIONS_SHOWN = 10
COST_BPS_EVALUATION = 5.0

VARIANT_COMPUTE = "compute"
VARIANT_PRICE_ONLY = "price-only"
VARIANTS = (VARIANT_COMPUTE, VARIANT_PRICE_ONLY)

STATUS_PROPOSED = "proposed"
STATUS_SUBMITTED = "submitted"
STATUS_FILLED = "filled"
STATUS_REJECTED = "rejected"
STATUS_SKIPPED = "skipped"
STATUSES = (STATUS_PROPOSED, STATUS_SUBMITTED, STATUS_FILLED, STATUS_REJECTED, STATUS_SKIPPED)

SIGNAL_KINDS = {"divergence": "price-change", "supply-regime": "pressure-change", "event-reaction": "event"}
HORIZON_DAYS = {"divergence": 20, "supply-regime": 20, "event-reaction": 10}
PRICE_EVENT_RETURN = 0.03
PRICE_EVENT_DAYS = 3


@dataclass(frozen=True)
class StrategySpec:
    id: str
    name: str
    objective: str
    rule: str
    parameters: dict
    universe: list[str]
    price_only_rule: str = ""


@dataclass(frozen=True)
class DecisionResult:
    t: datetime
    action: str
    ticker: str | None
    rationale: dict
    known_data_snapshot: dict
    mode: str
    acted: bool = False
    signal_kind: str | None = None
    signal_magnitude: float | None = None
    horizon_days: int | None = None
    proposed_weight: float | None = None
    variant: str = VARIANT_COMPUTE
    statement_class: str = "decision"

    @property
    def signal_id(self) -> str | None:
        if self.action == DecisionAction.HOLD.value or not self.ticker:
            return None
        return signal_id_for(self.rationale.get("strategyId", ""), self.ticker, self.t)


@dataclass(frozen=True)
class BacktestResult:
    start: datetime
    end: datetime
    trades: int
    total_return_pct: float
    benchmark_return_pct: float
    max_drawdown_pct: float
    hit_rate: float | None
    turnover: int
    decisions: list[DecisionResult]
    provenance: Provenance
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    variant: str = VARIANT_COMPUTE
    cost_bps: float = 0.0
    costs_usd: float = 0.0
    turnover_ratio: float = 0.0
    statement_class: str = "decision"
    # Closed buy-then-sell pairs: the hit rate's denominator (`trades` counts every fill, buys and sells).
    round_trips: int = 0
    # The instrument whose buy-and-hold `benchmark_return_pct` is: the strategy's first universe name.
    benchmark_ticker: str = ""


STRATEGIES: dict[str, StrategySpec] = {
    "divergence": StrategySpec(
        id="divergence", name="Compute / equity divergence",
        objective="Trade CRWV when the gap between compute prices and the CRWV and NBIS equity pair stretches.",
        rule="spread = 20d change of the H100 on-demand index minus the mean 20d change of CRWV and NBIS; "
             "z = spread z-score over the trailing 60 days; buy CRWV when z > +1.5 (the pair lags rising "
             "compute prices), sell CRWV when z < -1.5, hold otherwise. Only CRWV is traded; NBIS enters the spread only.",
        parameters={"window_days": 20, "zscore_window": 60, "enter_z": 1.5, "exit_z": -1.5, "index": INDEX_SERIES},
        universe=["CRWV", "NBIS"],
        price_only_rule="the same rule with the index's 20d change replaced by CRWV's own 20d change; only CRWV is traded",
    ),
    "supply-regime": StrategySpec(
        id="supply-regime", name="Supply-regime shift",
        objective="Detect a level shift in compute prices and position IREN, a power-first operator, accordingly.",
        rule="ratio = 20d mean / 60d mean of the H100 on-demand index; buy IREN when ratio > 1.05 (tightening), "
             "sell IREN when ratio < 0.95 (prices falling, oversupply regime), hold otherwise.",
        parameters={"fast_days": 20, "slow_days": 60, "sell_below": 0.95, "buy_above": 1.05, "index": INDEX_SERIES},
        universe=["IREN"],
        price_only_rule="the same rule with the index replaced by IREN's own closes",
    ),
    "event-reaction": StrategySpec(
        id="event-reaction", name="Infrastructure-event reaction",
        objective="React to verified announcements and earnings items for a covered name.",
        rule="buy a universe ticker named by a verified announcement or earnings item with severity >= 3 observed "
             "in the last 3 days, with one eighth of the remaining cash; sell it 10 calendar days after entry; hold otherwise.",
        parameters={"kinds": ["announcement", "earnings"], "min_severity": 3, "lookback_days": 3, "hold_days": 10},
        universe=["NVDA", "CRWV", "NBIS", "IREN", "APLD", "CORZ", "VRT", "SMCI"],
        price_only_rule=f"buy a universe ticker whose own {PRICE_EVENT_DAYS}-day return is at least {PRICE_EVENT_RETURN:.0%} (a price event in place of a feed event), with one eighth of the remaining cash; sell it 10 calendar days after entry",
    ),
}


def signal_id_for(strategy_id: str, ticker: str, at: datetime) -> str:
    return f"signal:{strategy_id}:{ticker}:{at.date().isoformat()}"


# -- knowledge at time t ---------------------------------------------------------

def _closes(data: SampleData, ticker: str, at: datetime) -> list[domain.PriceObservation]:
    series = data.series.get(f"equity:{ticker}:close")
    return point_in_time(series, at) if series else []


def _index(data: SampleData, at: datetime) -> list[domain.PriceObservation]:
    series = data.series.get(INDEX_SERIES)
    return point_in_time(series, at) if series else []


def _change(points: list[domain.PriceObservation], days: int) -> float | None:
    if len(points) <= days or points[-1 - days].value == 0:
        return None
    return points[-1].value / points[-1 - days].value - 1.0


def _result(spec: StrategySpec, at: datetime, action: str, ticker: str | None, rationale: dict, snapshot: dict, *,
            magnitude: float | None = None, variant: str = VARIANT_COMPUTE) -> DecisionResult:
    rationale = {**rationale, "strategyId": spec.id, "variant": variant}
    actionable = action != DecisionAction.HOLD.value and ticker is not None
    return DecisionResult(
        at, action, ticker, rationale, snapshot, StrategyMode.BACKTEST.value,
        signal_kind=SIGNAL_KINDS[spec.id] if actionable else None, signal_magnitude=magnitude if actionable else None,
        horizon_days=HORIZON_DAYS[spec.id] if actionable else None, proposed_weight=PROPOSED_WEIGHT if actionable else None,
        variant=variant,
    )


def _hold_reason(value: float, buy_above: float, sell_below: float, held: bool, *, inside: str = "within-band") -> str:
    """Why a two-threshold rule held, stated truthfully: a value past the buy threshold with the
    ticker already held is `already-held`, one past the sell threshold with nothing held is
    `nothing-held`; only a value between the thresholds is `inside` the band (falsifier V3 F14)."""
    if value > buy_above and held:
        return "already-held"
    if value < sell_below and not held:
        return "nothing-held"
    return inside


def decide(spec: StrategySpec, data: SampleData, at: datetime, *, holding: dict[str, datetime] | None = None,
           variant: str = VARIANT_COMPUTE) -> DecisionResult:
    """One decision for one strategy at time `at`, using only data known by `at`."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}")
    holding = holding or {}
    p = spec.parameters
    price_only = variant == VARIANT_PRICE_ONLY

    if spec.id == "divergence":
        window, zwin = p["window_days"], p["zscore_window"]
        ticker = spec.universe[0]
        basket = [_closes(data, t, at) for t in spec.universe]
        driver = _closes(data, ticker, at) if price_only else _index(data, at)
        spreads: list[float] = []
        n = min([len(driver)] + [len(b) for b in basket])
        for back in range(zwin, -1, -1):
            cut = n - back
            if cut <= window:
                continue
            drv_change = driver[cut - 1].value / driver[cut - 1 - window].value - 1.0
            eq_changes = [b[cut - 1].value / b[cut - 1 - window].value - 1.0 for b in basket]
            spreads.append(drv_change - statistics.fmean(eq_changes))
        snapshot = {"driver": f"equity:{ticker}:close" if price_only else INDEX_SERIES, "driverPoints": len(driver), "spreadSamples": len(spreads)}
        if len(spreads) < 10:
            return _result(spec, at, DecisionAction.HOLD.value, None, {"reason": "insufficient-history"}, snapshot, variant=variant)
        mean, std = statistics.fmean(spreads[:-1]), statistics.pstdev(spreads[:-1])
        z = (spreads[-1] - mean) / std if std else 0.0
        snapshot.update({"spread": round(spreads[-1], 5), "spreadMean": round(mean, 5), "spreadStd": round(std, 5), "z": round(z, 3)})
        if z > p["enter_z"] and ticker not in holding:
            return _result(spec, at, DecisionAction.BUY.value, ticker, {"signal": "z-above-enter", "z": round(z, 3)}, snapshot, magnitude=round(z, 3), variant=variant)
        if z < p["exit_z"] and ticker in holding:
            return _result(spec, at, DecisionAction.SELL.value, ticker, {"signal": "z-below-exit", "z": round(z, 3)}, snapshot, magnitude=round(z, 3), variant=variant)
        return _result(spec, at, DecisionAction.HOLD.value, ticker, {"signal": _hold_reason(z, p["enter_z"], p["exit_z"], ticker in holding), "z": round(z, 3)}, snapshot, variant=variant)

    if spec.id == "supply-regime":
        fast, slow = p["fast_days"], p["slow_days"]
        ticker = spec.universe[0]
        driver = _closes(data, ticker, at) if price_only else _index(data, at)
        snapshot = {"driver": f"equity:{ticker}:close" if price_only else INDEX_SERIES, "driverPoints": len(driver)}
        if len(driver) < slow:
            return _result(spec, at, DecisionAction.HOLD.value, None, {"reason": "insufficient-history"}, snapshot, variant=variant)
        values = [pt.value for pt in driver]
        ratio = statistics.fmean(values[-fast:]) / statistics.fmean(values[-slow:])
        snapshot.update({"fastMean": round(statistics.fmean(values[-fast:]), 4), "slowMean": round(statistics.fmean(values[-slow:]), 4), "ratio": round(ratio, 4)})
        if ratio > p["buy_above"] and ticker not in holding:
            return _result(spec, at, DecisionAction.BUY.value, ticker, {"signal": "tightening", "ratio": round(ratio, 4)}, snapshot, magnitude=round(ratio - 1.0, 4), variant=variant)
        if ratio < p["sell_below"] and ticker in holding:
            return _result(spec, at, DecisionAction.SELL.value, ticker, {"signal": "oversupply", "ratio": round(ratio, 4)}, snapshot, magnitude=round(ratio - 1.0, 4), variant=variant)
        hold = _hold_reason(ratio, p["buy_above"], p["sell_below"], ticker in holding, inside="no-regime-shift")
        return _result(spec, at, DecisionAction.HOLD.value, ticker, {"signal": hold, "ratio": round(ratio, 4)}, snapshot, variant=variant)

    if spec.id == "event-reaction":
        hold_days = p["hold_days"]
        for ticker, entered in holding.items():
            if (at - entered).days >= hold_days:
                return _result(spec, at, DecisionAction.SELL.value, ticker, {"signal": "hold-period-elapsed", "enteredAt": entered.isoformat()},
                               {"heldDays": (at - entered).days}, magnitude=float((at - entered).days), variant=variant)
        if price_only:
            moves = {}
            for ticker in spec.universe:
                if ticker in holding:
                    continue
                change = _change(_closes(data, ticker, at), PRICE_EVENT_DAYS)
                if change is not None:
                    moves[ticker] = round(change, 4)
            qualifying = {t: c for t, c in moves.items() if c >= PRICE_EVENT_RETURN}
            snapshot = {"returns3d": moves, "threshold": PRICE_EVENT_RETURN}
            if qualifying:
                ticker = max(qualifying, key=qualifying.get)
                return _result(spec, at, DecisionAction.BUY.value, ticker, {"signal": "price-event", "return3d": qualifying[ticker]}, snapshot, magnitude=qualifying[ticker], variant=variant)
            return _result(spec, at, DecisionAction.HOLD.value, None, {"signal": "no-qualifying-price-event"}, snapshot, variant=variant)
        since = at - timedelta(days=p["lookback_days"])
        qualifying = [
            item for item in data.feed
            if item.kind in p["kinds"] and item.classification == "verified" and item.severity >= p["min_severity"]
            and since <= item.observed_at <= at and any(t in spec.universe for t in item.entities.tickers)
        ]
        candidates = [item for item in qualifying if any(t in spec.universe and t not in holding for t in item.entities.tickers)]
        snapshot = {"candidates": [c.id for c in candidates], "lookbackFrom": since.isoformat()}
        if candidates:
            item = max(candidates, key=lambda c: (c.severity, c.observed_at))
            ticker = next(t for t in item.entities.tickers if t in spec.universe and t not in holding)
            return _result(spec, at, DecisionAction.BUY.value, ticker, {"signal": "verified-event", "itemId": item.id, "kind": item.kind, "severity": item.severity},
                           snapshot, magnitude=float(item.severity), variant=variant)
        if qualifying:
            # A qualifying event existed, but every ticker it names is already held: say so, rather
            # than that nothing qualified (falsifier R2-21).
            item = max(qualifying, key=lambda c: (c.severity, c.observed_at))
            held = next(t for t in item.entities.tickers if t in spec.universe)
            snapshot["heldEvents"] = [c.id for c in qualifying]
            return _result(spec, at, DecisionAction.HOLD.value, held, {"signal": "already-held", "itemId": item.id}, snapshot, variant=variant)
        return _result(spec, at, DecisionAction.HOLD.value, None, {"signal": "no-qualifying-event"}, snapshot, variant=variant)

    raise KeyError(spec.id)


# -- backtest --------------------------------------------------------------------

def driver_series(spec: StrategySpec, data: SampleData, variant: str) -> domain.Series:
    """The series whose dates a backtest walks: the compute index for the compute variant, the first universe
    name's own closes for the price-only twin (so a price-only run over real closes walks the real trading days,
    not an authored calendar), falling back to the index when that ticker has no closes at all."""
    if variant == VARIANT_PRICE_ONLY:
        closes = data.series.get(f"equity:{spec.universe[0]}:close")
        if closes is not None and closes.points:
            return closes
    return data.series[INDEX_SERIES]


def history_label(prov: Provenance) -> str:
    """How a backtest names the history it ran over: authored sample, or the observed source by name."""
    return "sample history" if effective_kind(prov) == "sample" else f"{prov.source} closes"


def backtest(spec: StrategySpec, data: SampleData, *, warmup_days: int = 61, variant: str = VARIANT_COMPUTE,
             cost_bps: float = 0.0) -> BacktestResult:
    """Long/flat simulation over the driver series' window; one position at a time per ticker.

    `cost_bps` is charged on every fill against the trader (a buy pays more, a
    sell receives less), so a costed run is comparable with the paper broker's
    5 bps slippage; `variant` selects the compute rule or its price-only twin.
    `data.series` is whatever the caller composed: the authored sample series,
    or real closes in their place where the archive holds them (terminal.py).
    """
    index = driver_series(spec, data, variant)
    dates = [pt.t for pt in index.points][warmup_days:]
    start_equity = 100_000.0
    cash, holding, entry_prices = start_equity, {}, {}
    quantities: dict[str, float] = {}
    equity_curve: list[tuple[datetime, float]] = []
    decisions: list[DecisionResult] = []
    round_trips: list[float] = []
    trades = 0
    costs = 0.0
    traded_notional = 0.0
    cost_factor = cost_bps / 10_000.0

    def close_at(ticker: str, at: datetime) -> float | None:
        pts = _closes(data, ticker, at)
        return pts[-1].value if pts else None

    for at in dates:
        decision = decide(spec, data, at, holding=holding, variant=variant)
        acted = False
        price = close_at(decision.ticker, at) if decision.ticker else None
        if decision.action == DecisionAction.BUY.value and price and decision.ticker not in holding:
            fill = price * (1.0 + cost_factor)
            qty = (cash / max(1, len(spec.universe) if spec.id == "event-reaction" else 1)) / fill if cash > 0 else 0
            qty = min(qty, cash / fill)
            if qty > 0:
                cash -= qty * fill
                costs += qty * (fill - price)
                traded_notional += qty * price
                holding[decision.ticker] = at
                entry_prices[decision.ticker] = fill
                quantities[decision.ticker] = qty
                trades += 1
                acted = True
        elif decision.action == DecisionAction.SELL.value and price and decision.ticker in holding:
            fill = price * (1.0 - cost_factor)
            qty = quantities.pop(decision.ticker)
            cash += qty * fill
            costs += qty * (price - fill)
            traded_notional += qty * price
            round_trips.append(fill / entry_prices.pop(decision.ticker) - 1.0)
            holding.pop(decision.ticker)
            trades += 1
            acted = True
        decisions.append(DecisionResult(**{**decision.__dict__, "acted": acted}))
        mark = cash + sum(q * (close_at(t, at) or entry_prices[t]) for t, q in quantities.items())
        equity_curve.append((at, mark))

    end_equity = equity_curve[-1][1] if equity_curve else start_equity
    peak, max_dd = start_equity, 0.0
    for _, value in equity_curve:
        peak = max(peak, value)
        max_dd = max(max_dd, (peak - value) / peak if peak else 0.0)
    bench_ticker = spec.universe[0]
    b0, b1 = close_at(bench_ticker, dates[0]), close_at(bench_ticker, dates[-1])
    benchmark = (b1 / b0 - 1.0) * 100.0 if b0 and b1 else 0.0
    hit_rate = (sum(1 for r in round_trips if r > 0) / len(round_trips)) if round_trips else None

    prov = derived(f"backtest:{spec.id}:{variant}", [index.points[-1].provenance], observed_at=dates[-1],
                   note=f"simulated over {history_label(index.points[-1].provenance)} at {cost_bps:g} bps per fill; no realised profit")
    return BacktestResult(
        start=dates[0], end=dates[-1], trades=trades,
        total_return_pct=round((end_equity / start_equity - 1.0) * 100.0, 3),
        benchmark_return_pct=round(benchmark, 3),
        max_drawdown_pct=round(max_dd * 100.0, 3),
        hit_rate=round(hit_rate, 3) if hit_rate is not None else None,
        turnover=trades,
        round_trips=len(round_trips),
        benchmark_ticker=bench_ticker,
        decisions=decisions[-DECISIONS_SHOWN:],
        provenance=prov,
        equity_curve=equity_curve,
        variant=variant, cost_bps=cost_bps, costs_usd=round(costs, 2), turnover_ratio=round(traded_notional / start_equity, 4),
    )


def equal_weight_benchmark(data: SampleData, tickers: list[str], start: datetime, end: datetime) -> dict:
    """Equal-weight buy-and-hold of `tickers` from `start` to `end`, from sample closes: the comparison every
    portfolio evaluation is shown against."""
    returns = {}
    for t in tickers:
        first = _closes(data, t, start)
        last = _closes(data, t, end)
        if first and last and first[-1].value:
            returns[t] = last[-1].value / first[-1].value - 1.0
    total = statistics.fmean(returns.values()) * 100.0 if returns else 0.0
    return {"tickers": sorted(returns), "start": start, "end": end, "total_return_pct": round(total, 3),
            "per_ticker_pct": {t: round(r * 100.0, 3) for t, r in sorted(returns.items())}, "rule": "equal-weight buy-and-hold, no costs"}


# -- forward: the decision ledger -----------------------------------------------------

def forward_decisions(db: Session, strategy_id: str | None = None, account: PaperAccount | None = None, limit: int = DECISIONS_SHOWN,
                      status: str | None = None) -> list[StrategyDecision]:
    # Forward decisions belong to one paper account. With no account there is nothing to
    # show: an unfiltered query here once served every account's proposals and fills to
    # anonymous readers and to other accounts (falsifier V3, 2026-11-07).
    if account is None:
        return []
    query = db.query(StrategyDecision).filter(StrategyDecision.mode == StrategyMode.FORWARD)
    if strategy_id is not None:
        query = query.filter(StrategyDecision.strategy_id == strategy_id)
    query = query.filter(StrategyDecision.account_id == account.id)
    if status is not None:
        query = query.filter(StrategyDecision.status == status)
    return query.order_by(StrategyDecision.t.desc(), StrategyDecision.id.desc()).limit(limit).all()


HOLD_REASONS_STATED = ("already-held", "nothing-held")


def _skip_reason(decision: DecisionResult) -> str:
    """Why a step recorded nothing to do. A hold because the ticker is already held (or, for a
    sell signal, nothing is held) says so; only a hold inside the rule's band is "no actionable
    signal" (falsifier R2-10)."""
    if decision.rationale.get("reason") == "insufficient-history":
        return "insufficient-sample"
    signal = decision.rationale.get("signal")
    if signal in HOLD_REASONS_STATED:
        return signal
    return "no-actionable-signal"


REPLAY_KEY = "replayOf"


def replay_of(row) -> str | None:
    """The sample date a decision replays (`rationale.replayOf`), or None for a step taken at its own
    time. A replay is hindsight: it is never a forward decision and never counts in forward results
    (falsifier R2-06)."""
    value = (row.rationale or {}).get(REPLAY_KEY) if row is not None else None
    return value if isinstance(value, str) else None


def is_replay(row) -> bool:
    return replay_of(row) is not None


def order_origin(strategy_id: str, replay: bool) -> str:
    """The origin a strategy's paper order carries: `strategy:<id>` for a forward decision,
    `replay:<id>` for a replayed one, so a desk or fund reader can tell the two apart."""
    return f"replay:{strategy_id}" if replay else f"strategy:{strategy_id}"


SKIP_SUPERSEDED = "superseded"
SKIP_EXPIRED = "expired"


def proposal_expires_at(row: StrategyDecision) -> datetime | None:
    """A proposal is a rule's view over the data known at `t`, for its horizon. Past `t + horizon`
    the rule's own horizon has elapsed and the proposal no longer says anything about today."""
    if row.horizon_days is None:
        return None
    t = row.t if row.t.tzinfo is not None else row.t.replace(tzinfo=timezone.utc)
    return t + timedelta(days=row.horizon_days)


def expire_proposals(db: Session, account: PaperAccount, now: datetime | None = None) -> int:
    """Marks this account's PROPOSED decisions whose horizon has elapsed as `skipped: expired`.
    Run before anything reads or acts on proposals, so a stale proposal is never offered or
    executed as if it were current (falsifier V3 F10). Returns how many rows it expired."""
    now = now or utcnow()
    rows = db.query(StrategyDecision).filter(StrategyDecision.account_id == account.id, StrategyDecision.mode == StrategyMode.FORWARD,
                                             StrategyDecision.status == STATUS_PROPOSED).all()
    expired = 0
    for row in rows:
        until = proposal_expires_at(row)
        if until is not None and now > until:
            row.status, row.skipped_reason = STATUS_SKIPPED, SKIP_EXPIRED
            expired += 1
    if expired:
        try:
            db.commit()
        except Exception:
            db.rollback()
            raise
    return expired


def step_forward(
    db: Session,
    spec: StrategySpec,
    data: SampleData,
    account: PaperAccount,
    price_lookup: broker.PriceLookup | None = None,
    *,
    at: datetime | None = None,
    replay: bool = False,
    now: datetime | None = None,
) -> tuple[StrategyDecision, PaperOrder | None]:
    """Decides on the data known at `at` (now by default) and records the decision as PROPOSED.
    Places nothing: the second element of the tuple is always None and exists so callers written
    against the older step-and-trade shape keep working; `accept_decision()` is the only path to
    an order.

    Forward steps and replays are separate ledgers that never supersede each other (falsifier
    R2-39): a forward step supersedes this strategy's earlier FORWARD proposals for the account,
    a replay its earlier REPLAY proposals. `replay=True` marks a step taken at a past date
    (`rationale.replayOf`), so every surface can say the decision is a replay of that date and not
    today's rule output; a replay of a date this strategy already has an open (proposed or
    submitted), filled or lapsed replay for returns that decision instead of recording another, so one
    date never piles decisions onto one signal (falsifier R2-06). A proposal whose horizon has
    already elapsed when it is made (a replay of an old date; `now` is the wall clock by default)
    is recorded as `skipped: expired`, never as proposed (falsifier R2-33)."""
    at = at or utcnow()
    now = now or utcnow()
    same_strategy = db.query(StrategyDecision).filter(StrategyDecision.account_id == account.id, StrategyDecision.strategy_id == spec.id,
                                                      StrategyDecision.mode == StrategyMode.FORWARD)
    if replay:
        expire_proposals(db, account, now)          # a lapsed replay is not "open": the date may be replayed afresh
        replayed = at.date().isoformat()
        # Open or filled, or lapsed (a fresh replay of that date would lapse at once just the same).
        existing = [r for r in same_strategy.filter(StrategyDecision.status.in_((STATUS_PROPOSED, STATUS_SUBMITTED, STATUS_FILLED, STATUS_SKIPPED))).all()
                    if replay_of(r) == replayed and (r.status != STATUS_SKIPPED or r.skipped_reason == SKIP_EXPIRED)]
        if existing:
            return max(existing, key=lambda r: r.id), None
    held = {p.ticker: p.updated_at for p in broker.positions(db, account)}
    decision = decide(spec, data, at, holding=held)
    actionable = decision.action != DecisionAction.HOLD.value and decision.ticker is not None
    rationale = {**decision.rationale, REPLAY_KEY: at.date().isoformat()} if replay else decision.rationale
    for old in same_strategy.filter(StrategyDecision.status == STATUS_PROPOSED).all():
        if is_replay(old) == replay:
            old.status, old.skipped_reason = STATUS_SKIPPED, SKIP_SUPERSEDED
    row = StrategyDecision(
        strategy_id=spec.id, account_id=account.id, t=at, action=DecisionAction(decision.action), ticker=decision.ticker,
        rationale=rationale, known_data_snapshot=decision.known_data_snapshot, mode=StrategyMode.FORWARD, acted=False,
        status=STATUS_PROPOSED if actionable else STATUS_SKIPPED,
        signal_id=signal_id_for(spec.id, decision.ticker, at) if actionable else None,
        signal_kind=decision.signal_kind, order_ids=None,
        skipped_reason=None if actionable else _skip_reason(decision),
        horizon_days=decision.horizon_days, proposed_weight=decision.proposed_weight,
    )
    until = proposal_expires_at(row) if actionable and replay else None
    if until is not None and now > until:
        row.status, row.skipped_reason = STATUS_SKIPPED, SKIP_EXPIRED
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    return row, None


def _refuse_unproposed(row: StrategyDecision) -> None:
    if row.status == STATUS_SKIPPED and row.skipped_reason == SKIP_SUPERSEDED:
        raise PaperOrderError(reason="decision-superseded", decision_id=row.id, status=row.status)
    if row.status == STATUS_SKIPPED and row.skipped_reason == SKIP_EXPIRED:
        raise PaperOrderError(reason="decision-expired", decision_id=row.id, status=row.status)
    raise PaperOrderError(reason="decision-not-proposed", decision_id=row.id, status=row.status)


def accept_decision(
    db: Session,
    spec: StrategySpec,
    account: PaperAccount,
    row: StrategyDecision,
    price_lookup: broker.PriceLookup,
    *,
    now: datetime | None = None,
) -> tuple[StrategyDecision, PaperOrder | None]:
    """Submits the paper order a PROPOSED decision calls for; the row's status follows the broker.

    Refuses a row that is not this account's (`decision-not-owned`), not this strategy's
    (`decision-strategy-mismatch`), superseded by a later step, past its horizon, or otherwise
    not proposed. The row is CLAIMED atomically (proposed -> submitted in one conditional
    UPDATE) before anything is placed, so of two concurrent accepts exactly one places an order
    and the other is refused with a 409 (falsifier V3 F16). A buy for a ticker already held is
    skipped as `risk-limit` (one position per ticker), a sell with nothing held as
    `strategy-not-followed`.
    """
    if row.account_id != account.id:
        raise PaperOrderError(reason="decision-not-owned", decision_id=row.id)
    if row.strategy_id != spec.id:
        raise PaperOrderError(reason="decision-strategy-mismatch", decision_id=row.id, strategy_id=row.strategy_id)
    expire_proposals(db, account, now)
    claimed = (
        db.query(StrategyDecision)
        .filter(StrategyDecision.id == row.id, StrategyDecision.account_id == account.id, StrategyDecision.status == STATUS_PROPOSED)
        .update({StrategyDecision.status: STATUS_SUBMITTED}, synchronize_session=False)
    )
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    if claimed != 1:
        if row.status == STATUS_SUBMITTED:
            # Another request holds the claim and is placing the order right now.
            raise DecisionConflictError(reason="decision-not-proposed", decision_id=row.id, status=row.status)
        _refuse_unproposed(row)

    held = {p.ticker for p in broker.positions(db, account)}
    order: PaperOrder | None = None
    try:
        if row.action == DecisionAction.BUY and row.ticker in held:
            row.status, row.skipped_reason = STATUS_SKIPPED, "risk-limit"
        elif row.action == DecisionAction.SELL and row.ticker not in held:
            row.status, row.skipped_reason = STATUS_SKIPPED, "strategy-not-followed"
        else:
            if row.action == DecisionAction.BUY:
                order = broker.place_order(db, account, ticker=row.ticker, side="buy", notional_usd=ORDER_NOTIONAL_USD,
                                           origin=order_origin(spec.id, is_replay(row)), price_lookup=price_lookup)
            else:
                order = broker.close_position(db, account, row.ticker, price_lookup, origin=order_origin(spec.id, is_replay(row)))
            row.order_ids = [str(order.id)]
            status = order.status.value if hasattr(order.status, "value") else order.status
            if status == "filled":
                row.status, row.acted = STATUS_FILLED, True
            elif status == "rejected":
                row.status, row.skipped_reason = STATUS_REJECTED, order.reason
        db.add(row)
        db.commit()
    except Exception:
        # Nothing was placed (or the broker refused to write): hand the claim back so the
        # proposal can be accepted again rather than sitting at `submitted` with no order.
        db.rollback()
        if order is None:
            db.query(StrategyDecision).filter(StrategyDecision.id == row.id, StrategyDecision.status == STATUS_SUBMITTED).update(
                {StrategyDecision.status: STATUS_PROPOSED}, synchronize_session=False)
            db.commit()
        raise
    db.refresh(row)
    return row, order
